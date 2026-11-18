"""Appwrite Inbound Email Function for automated Paytm/UPI payment verification"""
import re
import logging
from datetime import datetime
from typing import Dict, Any, Optional
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.services.users import Users
from pydantic import BaseModel, Field, field_validator, ValidationError
from .retry import with_exponential_backoff

logger = logging.getLogger("axelr.payment_processor")

class PaymentEmailData(BaseModel):
    """Pydantic schema for validated payment data extracted from incoming emails"""
    utr_number: str = Field(..., min_length=12, max_length=22, description="UPI transaction ID (UTR)")
    amount: float = Field(..., ge=1.0, description="Payment amount in INR")
    payer_vpa: str = Field(..., max_length=50, description="Payer's UPI VPA")
    payee_vpa: str = Field(..., max_length=50, description="Our business UPI VPA")
    transaction_timestamp: datetime = Field(..., description="Timestamp of the transaction")
    sender_email: str = Field(..., description="Email address of the sender (user)")
    email_subject: str = Field(..., description="Subject line of the payment confirmation email")
    raw_email_body: str = Field(..., max_length=100000, description="Full raw email body content")

    @field_validator('utr_number')
    def validate_utr(cls, v: str) -> str:
        """Validate UTR format - Indian bank UTR numbers follow specific patterns"""
        patterns = [
            r'^[A-Za-z0-9]{12,22}$',  # Generic UTR format
            r'^PAYTM[0-9]{16}$',       # Paytm specific format
            r'^UPI[0-9]{16}$'          # Generic UPI format
        ]
        if not any(re.match(pattern, v) for pattern in patterns):
            raise ValueError(f"Invalid UTR number format: {v}")
        return v.upper()

    @field_validator('payer_vpa', 'payee_vpa')
    def validate_vpa(cls, v: str) -> str:
        """Validate UPI VPA format"""
        if not re.match(r'^[a-zA-Z0-9._-]+@[a-zA-Z0-9]+$', v):
            raise ValueError(f"Invalid UPI VPA format: {v}")
        return v.lower()


class TransactionRecord(BaseModel):
    """Gateway-agnostic transaction schema for Appwrite database"""
    transaction_id: str = Field(..., description="Unique transaction ID (UTR for UPI, gateway ID for others)")
    user_id: Optional[str] = Field(None, description="Associated Appwrite user ID")
    user_email: str = Field(..., description="User's email address")
    amount: float = Field(..., description="Transaction amount")
    currency: str = Field(default="INR", description="Transaction currency")
    payment_gateway: str = Field(..., description="Payment gateway used (paytm_upi, jio, hyperswitch, etc.)")
    status: str = Field(default="pending", description="Transaction status: pending/verified/failed/duplicate")
    transaction_timestamp: datetime = Field(..., description="When transaction occurred")
    email_received_timestamp: datetime = Field(default_factory=datetime.utcnow, description="When we received the email")
    payer_details: Dict[str, Any] = Field(default_factory=dict, description="Payer metadata")
    verification_metadata: Dict[str, Any] = Field(default_factory=dict, description="Automation verification data")


class SubscriptionUpdate(BaseModel):
    """Subscription schema for Appwrite database"""
    user_id: str = Field(..., description="Appwrite user ID")
    credits_added: float = Field(..., description="Number of credits added to user account")
    subscription_tier: str = Field(..., description="Subscription tier activated")
    valid_until: datetime = Field(..., description="Subscription expiration timestamp")
    transaction_id: str = Field(..., description="Associated transaction ID")
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PaymentEmailProcessor:
    """
    Appwrite Inbound Email Function that automates Paytm/UPI payment verification
    by parsing confirmation emails, validating transaction details, and updating
    user subscriptions/credits in Appwrite databases.
    """
    
    def __init__(self):
        """Initialize Appwrite client and database connections"""
        self.client = Client()
        self.client.set_project(os.getenv("APPWRITE_PROJECT_ID"))
        self.client.set_key(os.getenv("APPWRITE_API_KEY"))
        self.client.set_endpoint(os.getenv("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1"))
        
        self.databases = Databases(self.client)
        self.users = Users(self.client)
        self.database_id = os.getenv("APPWRITE_DATABASE_ID")
        self.transactions_collection = os.getenv("APPWRITE_TRANSACTIONS_COLLECTION", "transactions")
        self.subscriptions_collection = os.getenv("APPWRITE_SUBSCRIPTIONS_COLLECTION", "subscriptions")
        
        # List of approved sender emails from banks/Paytm to prevent spoofing
        self.approved_senders = {
            "notify@paytm.in",
            "alerts@hdfcbank.net",
            "alerts@icicibank.com",
            "upialerts@oksbi.com",
            "notification@axlrbilling.ai"
        }
        
        # Our business VPA to verify payments are intended for us
        self.business_vpas = {
            os.getenv("BUSINESS_VPA", "axelr@paytm").lower(),
            os.getenv("BUSINESS_VPA_ALT", "contact@axelr").lower()
        }
        
        # Credit mapping: INR amount -> platform credits
        self.credit_conversion = {
            99: 100,    # ₹99 = 100 credits
            249: 300,   # ₹249 = 300 credits
            499: 700,   # ₹499 = 700 credits
            999: 1500   # ₹999 = 1500 credits
        }

    def extract_payment_data_from_email(self, email_payload: Dict[str, Any]) -> PaymentEmailData:
        """
        Extract and validate payment information from incoming email payload
        Supports Paytm and major Indian bank payment confirmation formats
        """
        subject = email_payload.get("subject", "")
        from_email = email_payload.get("from", "")
        body = email_payload.get("body", "")
        html_body = email_payload.get("html", "")
        
        # Combine text and HTML bodies for comprehensive parsing
        full_content = f"{body}\n{html_body}"
        
        # Extract UTR/transaction ID - common patterns in Indian bank emails
        utr_patterns = [
            r'UTR\s*(?:number|):\s*([A-Za-z0-9]{12,22})',
            r'Transaction ID:\s*([A-Za-z0-9]{12,22})',
            r'Ref\.? no\.\s*:\s*([A-Za-z0-9]{12,22})',
            r'PAYTM([0-9]{16})',
            r'UPI([0-9]{16})'
        ]
        
        utr = None
        for pattern in utr_patterns:
            match = re.search(pattern, full_content, re.IGNORECASE)
            if match:
                utr = match.group(1)
                break
        
        if not utr:
            raise ValueError("Could not extract valid UTR/transaction ID from email")
        
        # Extract amount
        amount_patterns = [
            r'Rs\.?\s*(\d+(?:\.\d{2})?)',
            r'INR\s*(\d+(?:\.\d{2})?)',
            r'₹\s*(\d+(?:\.\d{2})?)'
        ]
        
        amount = None
        for pattern in amount_patterns:
            match = re.search(pattern, full_content)
            if match:
                amount = float(match.group(1))
                break
        
        if not amount:
            raise ValueError("Could not extract valid payment amount from email")
        
        # Extract VPAs
        vpa_pattern = r'([a-zA-Z0-9._-]+@[a-zA-Z0-9]+)'
        vpas = re.findall(vpa_pattern, full_content)
        if len(vpas) < 2:
            raise ValueError(f"Could not find enough VPA addresses: found {len(vpas)}")
        
        payer_vpa = vpas[0]
        payee_vpa = vpas[1]
        
        # Extract transaction timestamp
        timestamp_patterns = [
            r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})',
            r'(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})'
        ]
        
        txn_timestamp = None
        for pattern in timestamp_patterns:
            match = re.search(pattern, full_content)
            if match:
                try:
                    txn_timestamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    txn_timestamp = datetime.strptime(match.group(1), "%d/%m/%Y %H:%M")
                break
        
        if not txn_timestamp:
            txn_timestamp = datetime.utcnow()
        
        return PaymentEmailData(
            utr_number=utr,
            amount=amount,
            payer_vpa=payer_vpa,
            payee_vpa=payee_vpa,
            transaction_timestamp=txn_timestamp,
            sender_email=from_email,
            email_subject=subject,
            raw_email_body=full_content
        )

    @with_exponential_backoff(max_retries=3)
    def check_duplicate_transaction(self, utr_number: str) -> bool:
        """Check if transaction has already been processed to prevent duplicates"""
        try:
            # Search for existing transaction with the same UTR
            response = self.databases.list_documents(
                database_id=self.database_id,
                collection_id=self.transactions_collection,
                queries=[f"transaction_id.equal({utr_number})"]
            )
            return len(response.get("documents", [])) > 0
        except Exception as e:
            logger.error(f"Error checking for duplicate transaction: {e}")
            raise  # Let retry logic handle temporary failures

    @with_exponential_backoff(max_retries=3)
    def create_transaction_record(self, payment_data: PaymentEmailData) -> str:
        """Create transaction record in Appwrite database"""
        transaction = TransactionRecord(
            transaction_id=payment_data.utr_number,
            user_email=payment_data.sender_email,
            amount=payment_data.amount,
            payment_gateway="paytm_upi",
            payer_details={
                "vpa": payment_data.payer_vpa,
                "email": payment_data.sender_email
            },
            transaction_timestamp=payment_data.transaction_timestamp,
            verification_metadata={
                "email_subject": payment_data.email_subject,
                "sender_email_verified": payment_data.sender_email in self.approved_senders,
                "payee_vpa_verified": payment_data.payee_vpa in self.business_vpas
            }
        )
        
        # Verify we're the intended payee
        if payment_data.payee_vpa not in self.business_vpas:
            transaction.status = "failed"
            logger.warning(f"Payment intended for wrong VPA: {payment_data.payee_vpa}. Our VPAs: {self.business_vpas}")
        
        # Verify sender is from approved domain to prevent spoofing
        if payment_data.sender_email not in self.approved_senders:
            transaction.status = "failed"
            logger.warning(f"Payment email from unapproved sender: {payment_data.sender_email}")
        
        response = self.databases.create_document(
            database_id=self.database_id,
            collection_id=self.transactions_collection,
            document_id=payment_data.utr_number,  # Use UTR as document ID for idempotency
            data=transaction.dict()
        )
        
        return response["$id"]

    @with_exponential_backoff(max_retries=5)
    def find_user_by_email(self, email: str) -> Optional[str]:
        """Find Appwrite user ID by email address"""
        try:
            response = self.users.list(search=email, limit=1)
            if response.get("total", 0) > 0:
                return response["users"][0]["$id"]
            return None
        except Exception as e:
            logger.error(f"Error finding user by email {email}: {e}")
            raise

    def calculate_credits(self, amount: float) -> tuple[float, str, datetime]:
        """Calculate credits, subscription tier, and validity period based on payment amount"""
        amount_int = int(round(amount))
        credits = self.credit_conversion.get(amount_int, amount_int)  # Default 1:1 if not in mapping
        
        # Determine subscription tier and validity
        if amount >= 999:
            tier = "premium_yearly"
            valid_until = datetime.utcnow().replace(year=datetime.utcnow().year + 1)
        elif amount >= 499:
            tier = "premium_monthly"
            valid_until = datetime.utcnow().replace(month=datetime.utcnow().month + 1)
        elif amount >= 249:
            tier = "pro_monthly"
            valid_until = datetime.utcnow().replace(month=datetime.utcnow().month + 1)
        else:
            tier = "basic_credits"
            valid_until = datetime.utcnow().replace(month=datetime.utcnow().month + 1)
        
        return credits, tier, valid_until

    @with_exponential_backoff(max_retries=3)
    def update_user_subscription(self, user_id: str, transaction_id: str, amount: float) -> None:
        """Update user's subscription and credit balance in Appwrite"""
        credits, tier, valid_until = self.calculate_credits(amount)
        
        subscription = SubscriptionUpdate(
            user_id=user_id,
            credits_added=credits,
            subscription_tier=tier,
            valid_until=valid_until,
            transaction_id=transaction_id
        )
        
        # Create subscription record
        self.databases.create_document(
            database_id=self.database_id,
            collection_id=self.subscriptions_collection,
            data=subscription.dict()
        )
        
        # Update user's metadata with current credit balance
        # First get existing user data
        user = self.users.get(user_id)
        current_credits = user.get("prefs", {}).get("credits", 0)
        new_credits = current_credits + credits
        
        self.users.update_prefs(
            user_id=user_id,
            prefs={
                "credits": new_credits,
                "subscription_tier": tier,
                "subscription_valid_until": valid_until.isoformat()
            }
        )
        
        logger.info(f"Successfully updated user {user_id}: +{credits} credits, tier: {tier}, valid until: {valid_until}")

    @with_exponential_backoff(max_retries=3)
    def mark_transaction_verified(self, transaction_id: str) -> None:
        """Mark transaction as verified after successful processing"""
        self.databases.update_document(
            database_id=self.database_id,
            collection_id=self.transactions_collection,
            document_id=transaction_id,
            data={
                "status": "verified",
                "verified_at": datetime.utcnow().isoformat()
            }
        )

    def process(self, context: Any, email_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Main entry point for Appwrite Function execution"""
        logger.info(f"Starting payment processing for email from: {email_payload.get('from')}")
        
        try:
            # Step 1: Extract and validate payment data from email
            payment_data = self.extract_payment_data_from_email(email_payload)
            logger.info(f"Successfully extracted payment data: UTR={payment_data.utr_number}, Amount=₹{payment_data.amount}")
            
            # Step 2: Idempotency check - avoid duplicate processing
            if self.check_duplicate_transaction(payment_data.utr_number):
                logger.warning(f"Duplicate transaction detected: UTR={payment_data.utr_number}")
                context.error("Duplicate transaction detected")
                return {"success": False, "error": "Duplicate transaction"}
            
            # Step 3: Create initial transaction record
            transaction_id = self.create_transaction_record(payment_data)
            logger.info(f"Created transaction record: {transaction_id}")
            
            # Step 4: Find associated user
            user_id = self.find_user_by_email(payment_data.sender_email)
            if not user_id:
                logger.error(f"Could not find user with email: {payment_data.sender_email}")
                context.error(f"User not found for email: {payment_data.sender_email}")
                return {"success": False, "error": "User not found"}
            
            # Update transaction with user ID
            self.databases.update_document(
                database_id=self.database_id,
                collection_id=self.transactions_collection,
                document_id=transaction_id,
                data={"user_id": user_id}
            )
            
            # Step 5: Update user's subscription and credits
            self.update_user_subscription(user_id, transaction_id, payment_data.amount)
            
            # Step 6: Mark transaction as fully verified
            self.mark_transaction_verified(transaction_id)
            
            logger.info(f"Payment processing completed successfully: UTR={payment_data.utr_number}")
            context.success("Payment processed successfully")
            return {
                "success": True,
                "transaction_id": transaction_id,
                "user_id": user_id,
                "utr": payment_data.utr_number
            }
            
        except ValidationError as e:
            logger.error(f"Payment data validation failed: {e}")
            context.error(f"Validation error: {str(e)}")
            return {"success": False, "error": f"Validation failed: {str(e)}"}
        except Exception as e:
            logger.critical(f"Unexpected error in payment processing: {e}", exc_info=True)
            context.error(f"Processing failed: {str(e)}")
            return {"success": False, "error": f"Processing failed: {str(e)}"}


# Entry point for Appwrite Function
def main(context: Any, email_payload: Dict[str, Any]) -> Dict[str, Any]:
    processor = PaymentEmailProcessor()
    return processor.process(context, email_payload)