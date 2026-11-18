"""Appwrite Storage Adapter for AXELR AI Unified Monorepo"""
from .schemas import (
    AppwriteAuditEvent,
    AppwriteDocument,
    AppwriteWebhookPayload,
    # New zero-trust and environment isolation schemas
    TeamMember,
    AccessApprovalRequest,
    EnvironmentCredentials,
    HealthCheckLog,
    AccessGrantLog,
    # Core payment and subscription schemas
    Transaction,
    Subscription,
    SupportTicket,
    # MCP integration schemas
    MCPSchemaInspectionRequest,
    MCPDatabaseMigration,
    MCPHealthAuditRequest,
    MCPAuditEvent,
    validate_webhook_payload
)
# New processing modules
from .payment_email_processor import PaymentEmailProcessor, main as payment_processor_main
from .support_triage_processor import SupportTriageProcessor, main as support_processor_main
from .database_migrations import DatabaseMigrator
from .zero_trust_onboarding import ZeroTrustOnboardingManager, main as onboarding_main
from .retry import with_exponential_backoff

__all__ = [
    # Core utilities
    "with_exponential_backoff",
    # Base schemas
    "AppwriteAuditEvent",
    "AppwriteDocument", 
    "AppwriteWebhookPayload",
    "validate_webhook_payload",
    # Business logic schemas
    "Transaction",
    "Subscription",
    "SupportTicket",
    # Zero-trust security schemas
    "TeamMember",
    "AccessApprovalRequest",
    "EnvironmentCredentials",
    "HealthCheckLog",
    "AccessGrantLog",
    # MCP integration schemas
    "MCPSchemaInspectionRequest",
    "MCPDatabaseMigration",
    "MCPHealthAuditRequest",
    "MCPAuditEvent",
    # Processing modules
    "PaymentEmailProcessor",
    "payment_processor_main",
    "SupportTriageProcessor",
    "support_processor_main",
    "DatabaseMigrator",
    "ZeroTrustOnboardingManager",
    "onboarding_main"
]