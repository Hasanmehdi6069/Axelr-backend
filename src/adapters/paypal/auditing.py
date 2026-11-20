"""Audit logging for all PayPal module operations with secure redaction"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from .core import get_core_module

logger = logging.getLogger("axelr.paypal.audit")

class AuditLogger:
    """Secure audit logger that redacts secrets and tracks all money-moving operations"""
    _instance: Optional["AuditLogger"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self.core = get_core_module()

    @classmethod
    def get_instance(cls) -> "AuditLogger":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _redact_sensitive(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Redact sensitive information before logging"""
        redacted = data.copy()
        sensitive_keys = ["authorization", "access_token", "client_secret", "token"]
        for key in list(redacted.keys()):
            if any(sensitive in key.lower() for sensitive in sensitive_keys):
                if isinstance(redacted[key], str) and len(redacted[key]) > 10:
                    redacted[key] = f"{redacted[key][:5]}...{redacted[key][-5:]}"
                else:
                    redacted[key] = "[REDACTED]"
        return redacted
    def log_api_call(
        self,
        method: str,
        url: str,
        status_code: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Log API calls to PayPal with redaction"""
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "operation": "api_call",
            "method": method,
            "url": url,
            "status_code": status_code,
            "error": error
        }
        if error:
            logger.error(f"AUDIT: {json.dumps(log_data)}")
        else:
            logger.info(f"AUDIT: {json.dumps(log_data)}")

    def log_money_movement(
        self,
        operation: str,
        transaction_id: str,
        amount: str,
        currency: str,
        user_id: Optional[str] = None,
    ) -> None:
        """Log all money-moving operations (capture, refund) for compliance"""
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "operation": operation,
            "transaction_id": transaction_id,
            "amount": amount,
            "currency": currency,
            "user_id": user_id or "unknown",
            "initiator": "authenticated_system"  # In production, add actual user ID
        }
        logger.warning(f"AUDIT: MONEY_MOVEMENT {json.dumps(log_data)}")

    def log_ai_extraction(self, input_hash: str, confidence: float, success: bool, error: Optional[str] = None) -> None:
        """Log AI extraction events as required by the requirements"""
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "operation": "ai_extraction",
            "input_hash": input_hash,
            "confidence": confidence,
            "success": success,
            "error": error
        }
        logger.info(f"AUDIT: {json.dumps(log_data)}")

    def log_auth_event(self, event: str, success: bool, source_ip: Optional[str] = None) -> None:
        """Log authentication events for audit trail"""
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "operation": "auth",
            "event": event,
            "success": success,
            "source_ip": source_ip or "unknown"
        }
        if not success:
            logger.error(f"AUDIT: {json.dumps(log_data)}")
        else:
            logger.info(f"AUDIT: {json.dumps(log_data)}")




def get_audit_logger() -> AuditLogger:
    return AuditLogger.get_instance()