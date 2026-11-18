"""Appwrite Inbound Email Function for AI-driven support ticket triage"""
import os
import re
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.services.messaging import Messaging
from pydantic import BaseModel, Field, field_validator
from .retry import with_exponential_backoff

logger = logging.getLogger("axelr.support_triage")

class SupportTicket(BaseModel):
    """Pydantic schema for support tickets stored in Appwrite database"""
    ticket_id: str = Field(..., description="Unique ticket identifier")
    user_email: str = Field(..., description="Submitter's email address")
    subject: str = Field(..., max_length=255, description="Email subject line")
    content: str = Field(..., max_length=100000, description="Full ticket content")
    category: str = Field(..., description="Automatically categorized ticket type")
    priority: str = Field(default="medium", description="Assigned priority level")
    status: str = Field(default="open", description="Ticket status: open/in_progress/resolved/closed")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict, description="AI analysis metadata")
    assigned_to: Optional[str] = Field(None, description="Team member assigned to ticket")
    user_id: Optional[str] = Field(None, description="Associated Appwrite user ID")

    @field_validator('category')
    def validate_category(cls, v: str) -> str:
        valid_categories = {"billing", "bug_report", "feature_request", "general_inquiry", "account_issue", "urgent_critical"}
        if v not in valid_categories:
            raise ValueError(f"Invalid category: {v}. Must be one of {valid_categories}")
        return v

    @field_validator('priority')
    def validate_priority(cls, v: str) -> str:
        valid_priorities = {"low", "medium", "high", "critical"}
        if v not in valid_priorities:
            raise ValueError(f"Invalid priority: {v}. Must be one of {valid_priorities}")
        return v


class TeamNotification(BaseModel):
    """Schema for team notification messages"""
    ticket_id: str
    category: str
    priority: str
    user_email: str
    subject: str
    summary: str
    channel: str = "slack"
    sent_at: datetime = Field(default_factory=datetime.utcnow)


class SupportTriageProcessor:
    """
    Appwrite Inbound Email Function that automatically triages support emails by:
    1. Categorizing incoming requests (Billing, Bug Report, Feature Request, etc.)
    2. Assigning priority levels based on content analysis
    3. Logging tickets into Appwrite databases
    4. Triggering automated team notifications to the correct channel
    """
    
    def __init__(self):
        """Initialize Appwrite client and services"""
        self.client = Client()
        self.client.set_project(os.getenv("APPWRITE_PROJECT_ID"))
        self.client.set_key(os.getenv("APPWRITE_API_KEY"))
        self.client.set_endpoint(os.getenv("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1"))
        
        self.databases = Databases(self.client)
        self.messaging = Messaging(self.client)
        self.database_id = os.getenv("APPWRITE_DATABASE_ID")
        self.support_tickets_collection = os.getenv("APPWRITE_SUPPORT_COLLECTION", "support_tickets")
        
        # Category classification rules - keywords that indicate specific categories
        self.category_patterns: Dict[str, List[str]] = {
            "billing": [
                r'bill', r'invoice', r'payment', r'charge', r'refund', r'credit',
                r'subscription', r'pricing', r'cost', r'money', r'utr', r'transaction'
            ],
            "bug_report": [
                r'bug', r'error', r'crash', r'broken', r'not working', r'failed',
                r'issue', r'problem', r'exception', r'traceback', r'cannot', r"can't"
            ],
            "feature_request": [
                r'feature', r'request', r'add', r'suggest', r'would be nice',
                r'could you', r'please add', r'implement', r'new functionality'
            ],
            "account_issue": [
                r'login', r'password', r'account', r'access', r'locked', r'verify',
                r'email', r'confirm', r'reset', r'2fa', r'mfa', r'authenticate'
            ],
            "urgent_critical": [
                r'urgent', r'emergency', r'critical', r'down', r'outage', 
                r'production', r'immediately', r'asap', r'urgent'
            ]
        }
        
        # Priority keywords - content that indicates higher priority
        self.priority_patterns: Dict[str, List[str]] = {
            "critical": [r'critical', r'emergency', r'production down', r'outage', r'breach'],
            "high": [r'urgent', r'asap', r'can\'t work', r'blocked', r'important'],
            "medium": [r'please', r'help', r'question', r'issue'],
            "low": [r'feedback', r'suggestion', r'nice to have', r'future']
        }
        
        # Team routing configuration - which team handles which categories
        self.team_routing: Dict[str, Dict[str, Any]] = {
            "billing": {"team": "finance", "slack_channel": "#billing-support", "email": "billing@axelr.ai"},
            "bug_report": {"team": "engineering", "slack_channel": "#engineering-support", "email": "eng@axelr.ai"},
            "feature_request": {"team": "product", "slack_channel": "#product-feedback", "email": "product@axelr.ai"},
            "account_issue": {"team": "support", "slack_channel": "#customer-support", "email": "support@axelr.ai"},
            "urgent_critical": {"team": "sre", "slack_channel": "#oncall-alerts", "email": "sre@axelr.ai"},
            "general_inquiry": {"team": "support", "slack_channel": "#customer-support", "email": "support@axelr.ai"}
        }
        
        # Appwrite Messaging topics for notifications
        self.notification_topics = {
            "finance": "finance-team",
            "engineering": "engineering-team", 
            "product": "product-team",
            "support": "support-team",
            "sre": "sre-oncall"
        }

    def classify_category(self, subject: str, content: str) -> Tuple[str, float]:
        """
        Classify support ticket into category using keyword matching with confidence score
        Returns (category, confidence)
        """
        full_text = f"{subject.lower()} {content.lower()}"
        category_scores = {}
        
        for category, patterns in self.category_patterns.items():
            score = 0
            for pattern in patterns:
                matches = len(re.findall(pattern, full_text, re.IGNORECASE))
                score += matches
            
            if score > 0:
                category_scores[category] = score
        
        if not category_scores:
            return "general_inquiry", 1.0
        
        # Find category with highest score
        max_category = max(category_scores.items(), key=lambda x: x[1])
        total = sum(category_scores.values())
        confidence = max_category[1] / total
        
        return max_category[0], confidence

    def assign_priority(self, subject: str, content: str, category: str) -> Tuple[str, float]:
        """
        Assign priority level to ticket based on content and category
        Returns (priority, confidence)
        """
        # Critical/urgent categories automatically get high priority
        if category == "urgent_critical":
            return "critical", 1.0
        
        full_text = f"{subject.lower()} {content.lower()}"
        priority_scores = {}
        
        for priority, patterns in self.priority_patterns.items():
            score = 0
            for pattern in patterns:
                matches = len(re.findall(pattern, full_text, re.IGNORECASE))
                score += matches
            
            if score > 0:
                priority_scores[priority] = score
        
        if not priority_scores:
            return "medium", 1.0
        
        max_priority = max(priority_scores.items(), key=lambda x: x[1])
        total = sum(priority_scores.values())
        confidence = max_priority[1] / total
        
        return max_priority[0], confidence

    def generate_summary(self, content: str, max_length: int = 500) -> str:
        """Generate a concise summary of the ticket for notifications"""
        # Clean up content - remove extra whitespace, signatures
        cleaned = re.sub(r'\s+', ' ', content.strip())
        if len(cleaned) <= max_length:
            return cleaned
        
        # Truncate and add ellipsis
        return cleaned[:max_length].rsplit(' ', 1)[0] + "..."

    @with_exponential_backoff(max_retries=3)
    def find_user_by_email(self, email: str) -> Optional[str]:
        """Find associated Appwrite user ID if submitter is a registered user"""
        from appwrite.services.users import Users
        users = Users(self.client)
        try:
            response = users.list(search=email, limit=1)
            if response.get("total", 0) > 0:
                return response["users"][0]["$id"]
            return None
        except Exception as e:
            logger.error(f"Error finding user by email {email}: {e}")
            return None

    @with_exponential_backoff(max_retries=3)
    def create_ticket_record(self, ticket: SupportTicket) -> str:
        """Create support ticket record in Appwrite database"""
        response = self.databases.create_document(
            database_id=self.database_id,
            collection_id=self.support_tickets_collection,
            data=ticket.dict()
        )
        return response["$id"]

    @with_exponential_backoff(max_retries=3)
    def send_team_notification(self, ticket: SupportTicket) -> None:
        """Send notification to the appropriate team based on ticket category"""
        routing = self.team_routing.get(ticket.category, self.team_routing["general_inquiry"])
        topic = self.notification_topics.get(routing["team"], self.notification_topics["support"])
        
        summary = self.generate_summary(ticket.content)
        notification = TeamNotification(
            ticket_id=ticket.ticket_id,
            category=ticket.category,
            priority=ticket.priority,
            user_email=ticket.user_email,
            subject=ticket.subject,
            summary=summary
        )
        
        # Create message payload for Appwrite Messaging
        message = {
            "topic": topic,
            "data": notification.dict(),
            "title": f"[{ticket.priority.upper()}] New {ticket.category.replace('_', ' ').title()} Ticket: {ticket.subject}",
            "body": summary
        }
        
        try:
            self.messaging.create_email(
                topic=topic,
                subject=message["title"],
                html_content=f"<p><strong>From:</strong> {ticket.user_email}</p><p><strong>Summary:</strong> {summary}</p><p>View full ticket in Appwrite console.</p>"
            )
            logger.info(f"Sent {ticket.category} ticket notification to {routing['team']} team: {ticket.ticket_id}")
        except Exception as e:
            logger.error(f"Failed to send team notification: {e}")
            raise

    def generate_auto_response(self, ticket: SupportTicket) -> str:
        """Generate personalized auto-response email to send back to the user"""
        responses = {
            "billing": "Thank you for contacting our billing team. We've received your inquiry and will respond within 24 business hours.",
            "bug_report": "Thank you for reporting this issue. Our engineering team has been notified and will investigate promptly.",
            "feature_request": "Thank you for your valuable suggestion! Our product team reviews all feedback regularly.",
            "account_issue": "Thank you for reaching out about your account. Our support team will help resolve your access issue shortly.",
            "urgent_critical": "Your urgent issue has been received and our SRE team is investigating immediately.",
            "general_inquiry": "Thank you for your message. A member of our team will get back to you within 48 hours."
        }
        
        base_response = responses.get(ticket.category, responses["general_inquiry"])
        ticket_id_line = f"Your ticket ID is: {ticket.ticket_id}"
        
        return f"""Thank you for contacting AXELR AI Support!

{base_response}

{ticket_id_line}

Best regards,
The AXELR AI Team
"""

    @with_exponential_backoff(max_retries=2)
    def send_auto_response(self, user_email: str, ticket_id: str, response_content: str) -> None:
        """Send automated acknowledgment email to the user"""
        try:
            self.messaging.create_email(
                emails=[user_email],
                subject=f"[AXELR Support] Your ticket has been received ({ticket_id})",
                html_content=f"<pre>{response_content}</pre>"
            )
            logger.info(f"Sent auto-response to {user_email} for ticket {ticket_id}")
        except Exception as e:
            logger.error(f"Failed to send auto-response: {e}")
            # Don't re-raise - auto-response failures shouldn't block ticket creation

    def process(self, context: Any, email_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Main entry point for Appwrite Function execution"""
        from_email = email_payload.get("from", "unknown@example.com")
        subject = email_payload.get("subject", "No Subject")
        body = email_payload.get("body", "")
        html_body = email_payload.get("html", "")
        full_content = f"{body}\n{html_body}"
        
        logger.info(f"Processing support ticket from {from_email}: {subject}")
        
        try:
            # Generate unique ticket ID
            import uuid
            ticket_uuid = str(uuid.uuid4())
            
            # Step 1: Classify ticket category and priority
            category, category_confidence = self.classify_category(subject, full_content)
            priority, priority_confidence = self.assign_priority(subject, full_content, category)
            
            logger.info(f"Ticket classification: category={category} ({category_confidence:.2f}), priority={priority} ({priority_confidence:.2f})")
            
            # Step 2: Find associated user if exists
            user_id = self.find_user_by_email(from_email)
            
            # Step 3: Create ticket record
            ticket = SupportTicket(
                ticket_id=ticket_uuid,
                user_email=from_email,
                subject=subject,
                content=full_content,
                category=category,
                priority=priority,
                user_id=user_id,
                metadata={
                    "category_confidence": category_confidence,
                    "priority_confidence": priority_confidence,
                    "classification_method": "keyword_based",
                    "email_raw": email_payload
                }
            )
            
            record_id = self.create_ticket_record(ticket)
            logger.info(f"Created support ticket record: {record_id}")
            
            # Step 4: Send team notification
            self.send_team_notification(ticket)
            
            # Step 5: Send auto-acknowledgment to user
            auto_response = self.generate_auto_response(ticket)
            self.send_auto_response(from_email, ticket_uuid, auto_response)
            
            logger.info(f"Support ticket processing completed successfully: {ticket_uuid}")
            context.success("Support ticket processed successfully")
            return {
                "success": True,
                "ticket_id": ticket_uuid,
                "category": category,
                "priority": priority,
                "user_id": user_id
            }
            
        except Exception as e:
            logger.critical(f"Unexpected error in support triage processing: {e}", exc_info=True)
            context.error(f"Processing failed: {str(e)}")
            return {"success": False, "error": f"Processing failed: {str(e)}"}


# Entry point for Appwrite Function
def main(context: Any, email_payload: Dict[str, Any]) -> Dict[str, Any]:
    processor = SupportTriageProcessor()
    return processor.process(context, email_payload)