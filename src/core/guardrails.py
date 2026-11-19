"""Input Schema Guardrails - Strict validation for all incoming user requests"""
import logging
import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("axelr.core.guardrails")

# Blocked patterns for content safety
BLOCKED_PATTERNS = [
    r"(ignore|bypass|skip).*previous",
    r"jailbreak",
    r"sudo|rm -rf|exec\(",
    r"__import__|eval\(|exec\(",
    r"drop table|select \* from",
]

class UserInputRequest(BaseModel):
    """Strict validation schema for all user input requests"""
    prompt: str = Field(..., min_length=1, max_length=32768, description="User's input prompt")
    user_id: str = Field(..., min_length=1, max_length=255, description="Unique user identifier")
    session_id: str = Field(..., min_length=1, max_length=255, description="Session identifier")
    context_window: Optional[int] = Field(4096, ge=1024, le=131072)
    model: Optional[str] = Field("llama3:70b")
    metadata: Optional[Dict[str, Any]] = Field(None)

    @field_validator('prompt')
    def validate_prompt_safety(cls, v: str) -> str:
        """Validate prompt against safety guardrails"""
        # Check length is reasonable
        if len(v.strip()) < 1:
            raise ValueError("Prompt cannot be empty")

        # Check for blocked patterns
        for pattern in BLOCKED_PATTERNS:
            if re.search(pattern, v, re.IGNORECASE):
                logger.warning(f"Blocked pattern detected in prompt: {pattern}")
                raise ValueError("Input contains disallowed content")

        return v.strip()

    @field_validator('model')
    def validate_model(cls, v: Optional[str]) -> str:
        """Validate requested model is allowed"""
        allowed_models = {"llama3:8b", "llama3:70b", "llama3.1:405b", "mistral:8x7b"}
        if v not in allowed_models:
            raise ValueError(f"Model {v} is not available. Allowed models: {allowed_models}")
        return v


class InputSchemaGuardrails:
    """Centralized input validation guardrails for all AXELR AI endpoints"""

    _instance: Optional["InputSchemaGuardrails"] = None

    def __init__(self):
        self.validation_count = 0
        self.blocked_count = 0
        logger.info("Input Schema Guardrails initialized")

    @classmethod
    def get_instance(cls) -> "InputSchemaGuardrails":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def validate_request(self, payload: Dict[str, Any]) -> UserInputRequest:
        """Validate an incoming request against all guardrails"""
        try:
            validated = UserInputRequest(**payload)
            self.validation_count += 1
            logger.debug(f"Request validated successfully - user: {validated.user_id}, session: {validated.session_id}")
            return validated
        except Exception as e:
            self.blocked_count += 1
            logger.error(f"Request blocked by guardrails: {str(e)}")
            raise ValueError(f"Input validation failed: {str(e)}") from e

    def get_stats(self) -> Dict[str, int]:
        """Get guardrails statistics"""
        return {
            "total_validations": self.validation_count,
            "total_blocked": self.blocked_count,
            "pass_rate": round((self.validation_count / (self.validation_count + self.blocked_count)) * 100, 2) if (self.validation_count + self.blocked_count) > 0 else 100.0
        }


def validate_input(payload: Dict[str, Any]) -> UserInputRequest:
    """Convenience function to validate input through guardrails"""
    guardrails = InputSchemaGuardrails.get_instance()
    return guardrails.validate_request(payload)
