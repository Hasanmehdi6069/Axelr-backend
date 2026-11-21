"""Apex Router local-tier client — Ollama + Gemma, zero cloud.

This is the ONLY module in the skill that performs network I/O. It hard-
enforces the UGOS "AI NAS Fit" constraint: the configured endpoint must
resolve to a loopback or RFC1918 private address. Any attempt to point the
skill at a public host raises CloudCallForbidden at construction time.

All inference runs on-device through Apex Router's local tier. No prompt,
document, or extracted metadata ever leaves the NAS.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger("axelr.ugreen_ai_nas.apex_client")

__all__ = [
    "ApexRouterClient",
    "CloudCallForbidden",
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODEL",
    "METADATA_SCHEMA",
    "normalize_metadata",
]

DEFAULT_ENDPOINT = os.getenv("APEX_ROUTER_ENDPOINT", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.getenv("APEX_ROUTER_MODEL", "gemma3:4b")
MAX_CONTENT_CHARS = 8000

_PRIVATE_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),  # unique local
    ipaddress.ip_network("fe80::/10"),  # link-local v6
)


class CloudCallForbidden(RuntimeError):
    """Raised when the configured Apex Router endpoint is not local.

    This is the enforceable guard behind UGOS constraint #2: the skill may
    only talk to Apex Router's on-device tier (Ollama + Gemma).
    """


def _is_local_host(host: str) -> bool:
    """Return True only for loopback / RFC1918 / link-local addresses."""
    if not host:
        return False
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False
        for info in infos:
            try:
                a = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if not any(a in net for net in _PRIVATE_NETS):
                return False
        return True
    return any(addr in net for net in _PRIVATE_NETS)


METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["document_type", "category", "confidence"],
    "properties": {
        "document_type": {
            "type": "string",
            "enum": ["invoice", "receipt", "email", "scan", "statement", "contract", "other"],
        },
        "date": {"type": ["string", "null"], "format": "date"},
        "sender": {"type": ["string", "null"], "maxLength": 200},
        "total_amount": {"type": ["number", "null"]},
        "currency": {"type": ["string", "null"], "pattern": "^[A-Z]{3}$"},
        "due_date": {"type": ["string", "null"], "format": "date"},
        "category": {
            "type": "string",
            "enum": ["Invoices", "Receipts", "Emails", "Contracts", "Statements", "Other"],
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "additionalProperties": False,
}

_DOC_TYPES = set(METADATA_SCHEMA["properties"]["document_type"]["enum"])
_CATEGORIES = set(METADATA_SCHEMA["properties"]["category"]["enum"])
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _coerce_date(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _DATE_RE.match(value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


def _coerce_amount(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.\-]", "", value)
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None


def _coerce_str(value: Any, limit: int = 200) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def normalize_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate + coerce a raw model payload into the canonical schema."""
    doc_type = str(payload.get("document_type") or "other").lower()
    if doc_type not in _DOC_TYPES:
        doc_type = "other"
    category = str(payload.get("category") or "Other")
    if category not in _CATEGORIES:
        # map by document type when the model picked a bad category
        category = {
            "invoice": "Invoices",
            "receipt": "Receipts",
            "email": "Emails",
            "contract": "Contracts",
            "statement": "Statements",
        }.get(doc_type, "Other")
    currency = payload.get("currency")
    if isinstance(currency, str):
        currency = currency.strip().upper()
    if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
        currency = None
    confidence = _coerce_amount(payload.get("confidence")) or 0.0
    confidence = max(0.0, min(1.0, float(confidence)))
    return {
        "document_type": doc_type,
        "date": _coerce_date(payload.get("date")),
        "sender": _coerce_str(payload.get("sender")),
        "total_amount": _coerce_amount(payload.get("total_amount")),
        "currency": currency,
        "due_date": _coerce_date(payload.get("due_date")),
        "category": category,
        "confidence": round(confidence, 3),
    }


def _fallback_metadata(filename: str) -> dict[str, Any]:
    """Deterministic, safe result when local inference is unavailable."""
    lower = filename.lower()
    doc_type = "invoice" if "invoice" in lower else "receipt" if "receipt" in lower else "other"
    return {
        "document_type": doc_type,
        "date": None,
        "sender": None,
        "total_amount": None,
        "currency": None,
        "due_date": None,
        "category": {"invoice": "Invoices", "receipt": "Receipts"}.get(doc_type, "Other"),
        "confidence": 0.0,
    }


def _parse_json_response(raw: str) -> dict[str, Any]:
    """Strip markdown fences and parse the first balanced JSON object."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object in response: {raw[:200]!r}")
    return json.loads(text[start : end + 1])


def _build_prompt(filename: str, content: str) -> str:
    """Strict, single-object JSON prompt for Gemma via Apex Router."""
    schema = json.dumps(METADATA_SCHEMA, indent=2)
    return (
        "You are Apex Router's on-device document-metadata extractor.\n"
        "You run entirely on the user's NAS. Never ask for network access.\n"
        "Return EXACTLY ONE JSON object matching this schema, and nothing else.\n"
        "If a field cannot be determined, use null. Do not guess.\n\n"
        f"Schema:\n{schema}\n\n"
        f"Filename: {filename}\n"
        "Document content:\n"
        '"""\n'
        f"{content[:MAX_CONTENT_CHARS]}\n"
        '"""\n'
    )


@dataclass
class ApexRouterClient:
    """Async client for Apex Router's local Ollama tier, with circuit breaker."""

    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    timeout_s: float = 90.0
    max_retries: int = 2
    backoff_base_s: float = 0.5
    circuit_failures: int = 3
    circuit_cooldown_s: float = 30.0
    _session: Optional[aiohttp.ClientSession] = field(default=None, repr=False)
    _failures: int = field(default=0, repr=False)
    _opened_until: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        host = parsed.hostname or ""
        if not _is_local_host(host):
            raise CloudCallForbidden(
                f"Refusing non-local Apex Router endpoint {self.endpoint!r}. "
                "UGOS constraint: no cloud inference, local tier only."
            )
        logger.info(
            "apex_router_local_only endpoint=%s model=%s verified_local=True",
            self.endpoint,
            self.model,
        )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_s),
                headers={"Content-Type": "application/json"},
            )
        return self._session

    async def _chat(self, prompt: str) -> str:
        now = time.time()
        if now < self._opened_until:
            raise RuntimeError(f"apex_router_circuit_open retry_in={self._opened_until - now:.1f}s")
        session = await self._ensure_session()
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0, "num_predict": 512},
        }
        try:
            async with session.post(f"{self.endpoint.rstrip('/')}/api/generate", json=body) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception:
            self._failures += 1
            if self._failures >= self.circuit_failures:
                self._opened_until = time.time() + self.circuit_cooldown_s
                logger.error(
                    "apex_router_circuit_opened failures=%d cooldown=%.0fs",
                    self._failures,
                    self.circuit_cooldown_s,
                )
            raise
        self._failures = 0
        text = (data or {}).get("response") or ""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("apex_router_empty_response")
        return text

    async def extract_metadata(self, filename: str, content: str) -> dict[str, Any]:
        """Run local extraction with retry, JSON parsing, and schema validation."""
        prompt = _build_prompt(filename, content)
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = await self._chat(prompt)
                payload = _parse_json_response(raw)
                return normalize_metadata(payload)
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "apex_extract_attempt_failed file=%s attempt=%d err=%s",
                    filename,
                    attempt + 1,
                    exc,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(self.backoff_base_s * (2**attempt))
        logger.error("apex_extract_exhausted file=%s err=%s", filename, last_err)
        return _fallback_metadata(filename)

    async def health(self) -> dict[str, Any]:
        """Lightweight liveness probe against the local Apex Router tier."""
        try:
            session = await self._ensure_session()
            async with session.get(
                f"{self.endpoint.rstrip('/')}/api/tags",
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as resp:
                return {
                    "ok": resp.status < 500,
                    "endpoint": self.endpoint,
                    "model": self.model,
                    "status": resp.status,
                    "circuit_open": time.time() < self._opened_until,
                }
        except Exception as exc:
            return {"ok": False, "endpoint": self.endpoint, "error": str(exc)}

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
