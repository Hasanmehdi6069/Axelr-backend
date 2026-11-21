"""Pydantic validation schemas for Appwrite adapter webhooks and data models"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field, ValidationInfo, field_validator


class PaymentGateway(str):
    """Gateway-agnostic payment processor type"""

    PAYTM_QR = "paytm_qr"
    UPI = "upi"
    JUSPAY = "juspay"
    HYPERSWITCH = "hyperswitch"
    PAYPAL = "paypal"  # For backward compatibility

    @classmethod
    def valid_gateways(cls) -> set[str]:
        return {cls.PAYTM_QR, cls.UPI, cls.JUSPAY, cls.HYPERSWITCH, cls.PAYPAL}


class TransactionStatus(str):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    REFUNDED = "refunded"
    DUPLICATE = "duplicate"

    @classmethod
    def valid_statuses(cls) -> set[str]:
        return {cls.PENDING, cls.VERIFIED, cls.FAILED, cls.REFUNDED, cls.DUPLICATE}


class Transaction(BaseModel):
    """Gateway-agnostic transaction schema for Appwrite - works with Paytm, Juspay, Hyperswitch"""

    transaction_id: str = Field(..., min_length=5, max_length=255, description="Unique transaction/UTR ID")
    user_id: str = Field(..., min_length=1, max_length=255, description="Associated user ID")
    amount: float = Field(..., gt=0, description="Payment amount in INR")
    currency: str = Field(default="INR", min_length=3, max_length=3)
    gateway: str = Field(..., description="Payment gateway used")
    status: str = Field(default=TransactionStatus.PENDING, description="Transaction status")
    utr: Optional[str] = Field(None, max_length=255, description="UPI transaction reference number (UTR)")
    payer_vpa: Optional[str] = Field(None, max_length=255, description="Payer's UPI VPA")
    payee_vpa: Optional[str] = Field(None, max_length=255, description="Merchant's UPI VPA")
    transaction_timestamp: datetime = Field(..., description="Original transaction timestamp")
    email_received_at: datetime = Field(
        default_factory=datetime.utcnow, description="When we received the confirmation email"
    )
    verified_at: Optional[datetime] = Field(None, description="When we verified the payment")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Gateway-specific metadata")
    email_source: Optional[str] = Field(
        None, max_length=255, description="Source email address (e.g., noreply@paytm.com)"
    )
    email_subject: Optional[str] = Field(None, max_length=512, description="Original email subject")

    @field_validator("gateway")
    def validate_gateway(cls, v: str) -> str:
        if v not in PaymentGateway.valid_gateways():
            raise ValueError(f"Invalid payment gateway: {v}. Must be one of {PaymentGateway.valid_gateways()}")
        return v

    @field_validator("status")
    def validate_status(cls, v: str) -> str:
        if v not in TransactionStatus.valid_statuses():
            raise ValueError(f"Invalid transaction status: {v}. Must be one of {TransactionStatus.valid_statuses()}")
        return v


class SubscriptionPlan(str):
    FREE = "free"
    BASIC = "basic"
    PRO = "pro"
    ENTERPRISE = "enterprise"

    @classmethod
    def valid_plans(cls) -> set[str]:
        return {cls.FREE, cls.BASIC, cls.PRO, cls.ENTERPRISE}


class Subscription(BaseModel):
    """User subscription schema - gateway agnostic"""

    user_id: str = Field(..., min_length=1, max_length=255)
    plan: str = Field(..., description="Subscription plan tier")
    status: str = Field(default="active", description="Subscription status (active, expired, canceled)")
    start_date: datetime = Field(default_factory=datetime.utcnow)
    end_date: datetime = Field(..., description="Subscription expiration date")
    last_payment_transaction_id: Optional[str] = Field(
        None, max_length=255, description="Reference to last successful transaction"
    )
    credits_remaining: Optional[float] = Field(None, description="Pay-as-you-go credits remaining")
    auto_renew: bool = Field(default=False, description="Whether subscription auto-renews")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("plan")
    def validate_plan(cls, v: str) -> str:
        if v not in SubscriptionPlan.valid_plans():
            raise ValueError(f"Invalid subscription plan: {v}. Must be one of {SubscriptionPlan.valid_plans()}")
        return v


class SupportTicket(BaseModel):
    """Customer support ticket schema for inbound emails"""

    ticket_id: str = Field(..., min_length=1, max_length=255)
    user_email: str = Field(..., description="Customer's email address")
    user_id: Optional[str] = Field(None, max_length=255, description="Associated user ID if identified")
    subject: str = Field(..., min_length=1, max_length=512)
    category: str = Field(..., description="Ticket category: billing, bug_report, feature_request, other")
    status: str = Field(default="open", description="Ticket status: open, in_progress, resolved, closed")
    priority: str = Field(default="medium", description="Ticket priority: low, medium, high, urgent")
    email_received_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = Field(None)
    assigned_to: Optional[str] = Field(None, max_length=255, description="Team member assigned to ticket")
    messages: List[Dict[str, Any]] = Field(default_factory=list, description="Conversation history")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)

    @field_validator("category")
    def validate_category(cls, v: str) -> str:
        valid_categories = {"billing", "bug_report", "feature_request", "other"}
        if v not in valid_categories:
            raise ValueError(f"Invalid ticket category: {v}. Must be one of {valid_categories}")
        return v


class AppwriteAuditEvent(BaseModel):
    """Pydantic schema for Appwrite audit events with strict validation"""

    kind: str = Field(..., min_length=1, max_length=64, description="Type of audit event")
    ts: datetime = Field(..., description="Timestamp of the event")
    user_id: Optional[str] = Field(None, max_length=255)
    pipeline_id: Optional[str] = Field(None, max_length=255)
    status: Optional[str] = Field(None, max_length=32)
    latency_ms: Optional[float] = Field(None, ge=0)
    payload: Optional[str] = Field(None, max_length=65535)
    day: Optional[str] = Field(None, max_length=10)

    @field_validator("payload")
    def validate_payload_json(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            try:
                import json

                json.loads(v)
            except json.JSONDecodeError:
                raise ValueError("Payload must be valid JSON string")
        return v


class AppwriteDocument(BaseModel):
    """Pydantic schema for indexed documents stored in Appwrite"""

    doc_id: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., min_length=1, max_length=1048576)
    meta: Optional[str] = Field(None, max_length=65535)
    ts: datetime = Field(...)

    @field_validator("meta")
    def validate_meta_json(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            try:
                import json

                json.loads(v)
            except json.JSONDecodeError:
                raise ValueError("Metadata must be valid JSON string")
        return v


class AppwriteWebhookPayload(BaseModel):
    """Strict Pydantic validation for incoming Appwrite webhooks"""

    event: str = Field(..., min_length=1, max_length=128)
    id: str = Field(..., min_length=1, max_length=255, alias="$id")
    createdAt: datetime = Field(..., alias="$createdAt")
    updatedAt: Optional[datetime] = Field(None, alias="$updatedAt")
    data: Dict[str, Any] = Field(..., description="Webhook payload data")
    user_id: Optional[str] = Field(None, max_length=255)

    model_config = {
        "populate_by_name": True,
        "extra": "forbid",  # Strictly reject unexpected fields
    }


class PayPalWebhookPayload(BaseModel):
    """Strict Pydantic validation for PayPal webhook payloads"""

    id: str = Field(..., min_length=1, max_length=255)
    event_type: str = Field(..., min_length=1, max_length=128)
    resource: Dict[str, Any] = Field(...)
    summary: Optional[str] = Field(None, max_length=1024)
    create_time: Optional[datetime] = None
    links: Optional[List[Dict[str, Any]]] = None

    @field_validator("event_type")
    def validate_paypal_event_type(cls, v: str) -> str:
        valid_events = {
            "CHECKOUT.ORDER.APPROVED",
            "PAYMENT.CAPTURE.COMPLETED",
            "PAYMENT.CAPTURE.REFUNDED",
            "PAYMENT.CAPTURE.DENIED",
        }
        if v not in valid_events:
            raise ValueError(f"Unsupported PayPal event type: {v}. Must be one of {valid_events}")
        return v


# Zero-Trust Security Models
class TeamMember(BaseModel):
    """Team member model for zero-trust onboarding"""

    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=100)
    role_type: str = Field(..., description="Requested role type")
    environment: str = Field(default="staging", description="Deployment environment access requested")
    is_contractor: bool = Field(default=False, description="Whether the member is a contractor")
    start_date: datetime = Field(default_factory=datetime.utcnow)
    end_date: Optional[datetime] = None  # For contractors, required expiration
    request_reason: str = Field(..., min_length=20, max_length=500, description="Justification for access")

    @field_validator("end_date")
    def validate_contractor_end_date(cls, v: Optional[datetime], values: ValidationInfo) -> Optional[datetime]:
        if values.data.get("is_contractor") and v is None:
            raise ValueError("Contractors must have an end date for access expiration")
        if v and v < datetime.utcnow():
            raise ValueError("End date must be in the future")
        return v

    @field_validator("role_type")
    def validate_role_type(cls, v: str) -> str:
        valid_roles = {
            "system",
            "admin",
            "billing_admin",
            "finance",
            "developer_staging",
            "developer_production",
            "support",
            "contractor",
        }
        if v not in valid_roles:
            raise ValueError(f"Invalid role type. Must be one of: {valid_roles}")
        return v


class AccessApprovalRequest(BaseModel):
    """Model for tracking access approval requests"""

    member_email: EmailStr
    requested_role: str
    requested_environment: str
    is_contractor: bool
    request_reason: str
    status: str = Field(default="pending_approval")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    expiration_date: Optional[datetime] = None


class EnvironmentCredentials(BaseModel):
    """Model for isolated environment credentials"""

    project_id: str
    api_key: str
    environment: str = Field(..., pattern="^(staging|production)$")
    database_id: str
    functions_enabled: bool = True
    ssh_keys: List[str] = Field(default_factory=list)

    @field_validator("ssh_keys")
    def validate_production_ssh_keys(cls, v: List[str], values: ValidationInfo) -> List[str]:
        if values.data.get("environment") == "production" and len(v) < 1:
            raise ValueError("Production environment requires explicit SSH key registration")
        return v


class HealthCheckLog(BaseModel):
    """Model for logging instance health checks"""

    environment: str
    is_healthy: bool
    check_timestamp: datetime = Field(default_factory=datetime.utcnow)
    details: Optional[Dict[str, Any]] = None


class AccessGrantLog(BaseModel):
    """Model for logging environment access grants"""

    user_id: str
    environment: str
    access_type: str
    request_reason: str
    granted_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime


# MCP Integration Models
class MCPSchemaInspectionRequest(BaseModel):
    """Model for MCP schema inspection requests"""

    database_id: str
    collection_ids: Optional[List[str]] = None
    include_permissions: bool = Field(default=True)
    include_indexes: bool = Field(default=True)


class MCPDatabaseMigration(BaseModel):
    """Model for MCP database migration requests"""

    migration_name: str
    environment: str = Field(..., pattern="^(staging|production)$")
    collections_to_create: List[Dict[str, Any]] = Field(default_factory=list)
    collections_to_update: List[Dict[str, Any]] = Field(default_factory=list)
    dry_run: bool = Field(default=True)


class MCPHealthAuditRequest(BaseModel):
    """Model for MCP health audit requests"""

    check_appwrite: bool = True
    check_vultr: bool = True
    check_resources: List[str] = Field(default_factory=lambda: ["instances", "databases", "storage"])
    generate_report: bool = True


class MCPAuditEvent(BaseModel):
    """Model for logging MCP events"""

    event_type: str
    details: Dict[str, Any]
    timestamp: datetime = Field(default_factory=datetime.utcnow)


def validate_webhook_payload(payload: dict, schema: type[BaseModel]) -> BaseModel:
    """Validate any webhook payload against its Pydantic schema"""
    try:
        return schema(**payload)
    except Exception as e:
        import logging

        logger = logging.getLogger("axelr.schemas")
        logger.error(f"Webhook validation failed: {e}")
        raise
