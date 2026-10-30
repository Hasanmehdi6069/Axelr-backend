# core/gateway_translate.py
"""
AXELR — Provider Payload Translator
=====================================
Cross-provider payload translation between the three wire shapes used by
the 40+ provider cascade:

    OpenAI     — {model, messages[{role, content}], max_tokens, temperature}
    Anthropic  — {model, messages[{role, content}], max_tokens,
                  temperature, system, anthropic_version}
    Gemini     — {contents[{role, parts[{text}]}], generationConfig{...}}

Every function is pure, deterministic, and never raises.

Public:
    to_openai(prompt, model, max_tokens, temp, system)     -> dict
    to_anthropic(prompt, model, max_tokens, temp, system)  -> dict
    to_gemini(prompt, model, max_tokens, temp, system)     -> dict
    from_openai_response(body)   -> str
    from_anthropic_response(body)-> str
    from_gemini_response(body)   -> str
    detect_shape(url_or_provider) -> str
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("axelr.gateway_translate")


# ═══════════════════════════════════════════════════════════════════════════
# Outbound — our canonical message list → provider shape
# ═══════════════════════════════════════════════════════════════════════════
def _split_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """Separate a system message (if any) from the conversation turns."""
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for m in messages or []:
        role = str(m.get("role", "user"))
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        content = str(content or "")
        if role == "system":
            system_parts.append(content)
        else:
            turns.append({"role": role, "content": content})
    return "\n\n".join(system_parts).strip(), turns


def to_openai(
    messages: list[dict[str, Any]], *, model: str,
    max_tokens: int, temp: float, system: str = "",
    stream: bool = False,
) -> dict[str, Any]:
    """Canonical OpenAI chat-completions payload."""
    out_messages: list[dict[str, str]] = []
    if system:
        out_messages.append({"role": "system", "content": system})
    out_messages.extend(messages or [])
    return {
        "model": model,
        "messages": out_messages,
        "max_tokens": int(max_tokens),
        "temperature": float(temp),
        "stream": bool(stream),
    }


def to_anthropic(
    messages: list[dict[str, Any]], *, model: str,
    max_tokens: int, temp: float, system: str = "",
    stream: bool = False,
) -> dict[str, Any]:
    """
    Anthropic /v1/messages payload. Anthropic takes `system` as a
    top-level field, not a message role. Roles are only user|assistant.
    """
    sys_extra, turns = _split_messages(messages)
    merged_system = "\n\n".join(filter(None, [system, sys_extra]))
    cleaned: list[dict[str, str]] = []
    for t in turns:
        role = t.get("role", "user")
        if role not in ("user", "assistant"):
            role = "user"
        cleaned.append({"role": role, "content": t.get("content", "")})
    if not cleaned:
        cleaned = [{"role": "user", "content": ""}]
    payload: dict[str, Any] = {
        "model": model,
        "messages": cleaned,
        "max_tokens": int(max_tokens),
        "temperature": float(temp),
        "stream": bool(stream),
    }
    if merged_system:
        payload["system"] = merged_system
    return payload


def to_gemini(
    messages: list[dict[str, Any]], *, model: str,
    max_tokens: int, temp: float, system: str = "",
    stream: bool = False,
) -> dict[str, Any]:
    """
    Google Gemini v1beta generateContent / streamGenerateContent payload.
    System goes into `systemInstruction`. Conversation turns use
    role user|model (NOT assistant).
    """
    sys_extra, turns = _split_messages(messages)
    merged_system = "\n\n".join(filter(None, [system, sys_extra]))
    contents: list[dict[str, Any]] = []
    for t in turns:
        role = t.get("role", "user")
        if role == "assistant":
            role = "model"
        contents.append({"role": role, "parts": [{"text": t.get("content", "")}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]
    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": int(max_tokens),
            "temperature": float(temp),
        },
    }
    if merged_system:
        payload["systemInstruction"] = {"parts": [{"text": merged_system}]}
    return payload


# ═══════════════════════════════════════════════════════════════════════════
# Inbound — provider response body → plain text
# ═══════════════════════════════════════════════════════════════════════════
def from_openai_response(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices") or []
    if not choices:
        return ""
    first = choices[0] or {}
    msg = first.get("message") or {}
    text = msg.get("content")
    if isinstance(text, list):
        text = "".join(
            p.get("text", "") for p in text if isinstance(p, dict)
        )
    return str(text or "")


def from_anthropic_response(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    content = body.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def from_gemini_response(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    candidates = body.get("candidates") or []
    if not candidates:
        return ""
    parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
    return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))


# ═══════════════════════════════════════════════════════════════════════════
# Shape detection
# ═══════════════════════════════════════════════════════════════════════════
_ANTHROPIC_HINTS = ("anthropic", "/v1/messages", "claude")
_GEMINI_HINTS = ("gemini", "generativelanguage", "googleapis")


def detect_shape(url_or_provider: str) -> str:
    """Return 'openai' | 'anthropic' | 'gemini'. Never raises."""
    low = (url_or_provider or "").lower()
    if any(h in low for h in _ANTHROPIC_HINTS):
        return "anthropic"
    if any(h in low for h in _GEMINI_HINTS):
        return "gemini"
    return "openai"


def build_payload(
    shape: str, messages: list[dict[str, Any]], *,
    model: str, max_tokens: int, temp: float, system: str = "",
    stream: bool = False,
) -> dict[str, Any]:
    if shape == "anthropic":
        return to_anthropic(messages, model=model, max_tokens=max_tokens,
                            temp=temp, system=system, stream=stream)
    if shape == "gemini":
        return to_gemini(messages, model=model, max_tokens=max_tokens,
                         temp=temp, system=system, stream=stream)
    return to_openai(messages, model=model, max_tokens=max_tokens,
                     temp=temp, system=system, stream=stream)


def parse_response(shape: str, body: Any) -> str:
    if shape == "anthropic":
        return from_anthropic_response(body)
    if shape == "gemini":
        return from_gemini_response(body)
    return from_openai_response(body)


__all__ = [
    "to_openai", "to_anthropic", "to_gemini",
    "from_openai_response", "from_anthropic_response", "from_gemini_response",
    "detect_shape", "build_payload", "parse_response",
]