"""Strict Pydantic validation schemas for Nebius Cloud inference API"""

from typing import Any, Dict, List

from pydantic import BaseModel, Field, ValidationError, field_validator


class ChatMessage(BaseModel):
    """Chat message format for Llama 3 inference"""

    role: str = Field(..., description="Message role (system, user, assistant)")
    content: str = Field(..., min_length=1, description="Message content")

    @field_validator("role")
    def validate_role(cls, v: str) -> str:
        valid_roles = {"system", "user", "assistant"}
        if v not in valid_roles:
            raise ValueError(f"Invalid role: {v}. Must be one of {valid_roles}")
        return v


class NebiusChatCompletionRequest(BaseModel):
    """Strict validation for chat completion requests to Nebius API"""

    model: str = Field(..., description="Model identifier (e.g., llama3:70b)")
    messages: List[ChatMessage] = Field(..., min_length=1, description="Conversation messages")
    max_tokens: int = Field(1024, ge=1, le=32768, description="Maximum tokens to generate")
    temperature: float = Field(0.7, ge=0.0, le=2.0, description="Sampling temperature")
    top_p: float = Field(0.95, ge=0.0, le=1.0, description="Nucleus sampling threshold")
    stream: bool = Field(False, description="Enable streaming responses")
    presence_penalty: float = Field(0.0, ge=-2.0, le=2.0)
    frequency_penalty: float = Field(0.0, ge=-2.0, le=2.0)

    @field_validator("model")
    def validate_model(cls, v: str) -> str:
        valid_models = {"llama3:8b", "llama3:70b", "llama3.1:405b", "mistral:8x7b"}
        if v not in valid_models:
            raise ValueError(f"Unsupported model: {v}. Must be one of {valid_models}")
        return v


class Choice(BaseModel):
    """Individual completion choice from the model"""

    index: int
    message: ChatMessage
    finish_reason: str = Field(..., description="Why generation stopped")


class Usage(BaseModel):
    """Token usage statistics"""

    prompt_tokens: int = Field(..., ge=0)
    completion_tokens: int = Field(..., ge=0)
    total_tokens: int = Field(..., ge=0)


class NebiusChatCompletionResponse(BaseModel):
    """Validated response format from Nebius inference API"""

    id: str
    object: str
    created: int
    model: str
    choices: List[Choice]
    usage: Usage


def validate_inference_request(payload: Dict[str, Any]) -> NebiusChatCompletionRequest:
    """Validate an inference request before sending to Nebius"""
    try:
        return NebiusChatCompletionRequest(**payload)
    except ValidationError as e:
        import logging

        logger = logging.getLogger("axelr.nebius.schemas")
        logger.error(f"Inference request validation failed: {e}")
        raise ValueError(f"Invalid inference request: {str(e)}") from e
