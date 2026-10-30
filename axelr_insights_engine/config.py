"""Environment-driven configuration — single source of truth.

All settings load once at import. Invalid values log a warning and fall
back to safe defaults so a typo in a deployment env var never crashes
startup. No secrets have defaults here; a missing APPWRITE_API_KEY is
caught at backend-init time, not silently defaulted.

12-factor rules honoured:
  - Config lives in the environment, not in code.
  - Precedence: environment only (no layered fallbacks).
  - Type-safe parsing with explicit ranges.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("insights.config")


def _str(key: str, default: str = "",
         *, allowed: tuple[str, ...] | None = None) -> str:
    v = (os.getenv(key) or default).strip()
    if allowed and v not in allowed:
        logger.warning("invalid %s=%r; allowed=%s; using %r",
                       key, v, allowed, default)
        return default
    return v


def _int(key: str, default: int,
         *, lo: int | None = None, hi: int | None = None) -> int:
    try:
        v = int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        logger.warning("invalid int %s; using %d", key, default)
        return default
    if lo is not None and v < lo:
        logger.warning("%s=%d below min %d; clamped", key, v, lo)
        return lo
    if hi is not None and v > hi:
        logger.warning("%s=%d above max %d; clamped", key, v, hi)
        return hi
    return v


def _float(key: str, default: float,
           *, lo: float | None = None, hi: float | None = None) -> float:
    try:
        v = float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        logger.warning("invalid float %s; using %s", key, default)
        return default
    if lo is not None and v < lo:
        return lo
    if hi is not None and v > hi:
        return hi
    return v


def _bool(key: str, default: bool) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "t", "y")


def _csv(key: str) -> frozenset[str]:
    raw = os.getenv(key) or ""
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    """Immutable, validated configuration for the entire engine."""

    storage_backend: Literal["mongo", "appwrite"]
    search_backend: Literal["tfidf", "sentence-transformer"]

    mongodb_uri: str
    mongodb_db: str

    appwrite_endpoint: str
    appwrite_project_id: str
    appwrite_api_key: str
    appwrite_database_id: str

    search_top_k_default: int
    search_top_k_max: int
    search_min_score: float
    search_cache_ttl: int
    search_max_docs: int

    audit_retention_days: int
    audit_batch_max: int

    admin_emails: frozenset[str]

    seed_demo: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            storage_backend=_str(
                "INSIGHTS_STORAGE_BACKEND", "mongo",
                allowed=("mongo", "appwrite"),
            ),
            search_backend=_str(
                "INSIGHTS_SEARCH_BACKEND", "tfidf",
                allowed=("tfidf", "sentence-transformer"),
            ),
            mongodb_uri=_str("MONGODB_URI"),
            mongodb_db=_str("MONGODB_DB_NAME", "axelr"),
            appwrite_endpoint=_str("APPWRITE_ENDPOINT"),
            appwrite_project_id=_str("APPWRITE_PROJECT_ID"),
            appwrite_api_key=_str("APPWRITE_API_KEY"),
            appwrite_database_id=_str("APPWRITE_DATABASE_ID", "insights"),
            search_top_k_default=_int("INSIGHTS_SEARCH_TOP_K_DEFAULT", 5, lo=1, hi=100),
            search_top_k_max=_int("INSIGHTS_SEARCH_TOP_K_MAX", 20, lo=5, hi=200),
            search_min_score=_float("INSIGHTS_SEARCH_MIN_SCORE", 0.05, lo=0.0, hi=1.0),
            search_cache_ttl=_int("INSIGHTS_SEARCH_CACHE_TTL", 300, lo=0, hi=3600),
            search_max_docs=_int("INSIGHTS_SEARCH_MAX_DOCUMENTS", 5000,
                                 lo=100, hi=1_000_000),
            audit_retention_days=_int("INSIGHTS_AUDIT_RETENTION_DAYS", 30,
                                      lo=1, hi=3650),
            audit_batch_max=_int("INSIGHTS_AUDIT_BATCH_MAX", 100, lo=1, hi=1000),
            admin_emails=_csv("INSIGHTS_ADMIN_EMAILS"),
            seed_demo=_bool("INSIGHTS_SEED_DEMO", True),
        )

    def appwrite_configured(self) -> bool:
        return bool(
            self.appwrite_endpoint
            and self.appwrite_project_id
            and self.appwrite_api_key
        )

    def mongo_configured(self) -> bool:
        return bool(self.mongodb_uri)

    def __repr__(self) -> str:
        # Never leak keys/URIs in logs.
        return (
            f"Settings(storage={self.storage_backend!r}, "
            f"search={self.search_backend!r}, "
            f"retention={self.audit_retention_days}d, "
            f"max_docs={self.search_max_docs})"
        )


settings = Settings.from_env()
logger.info("config loaded: %r", settings)