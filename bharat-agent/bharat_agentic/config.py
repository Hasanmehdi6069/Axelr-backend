"""Fail-fast configuration.

Reads env vars once at startup. Any invalid value raises `ConfigError`
*immediately* — no silent defaults that bite in production.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from .errors import ConfigError


@dataclass(frozen=True, slots=True)
class EngineConfig:
    # Storage
    db_path: str = "./bharat_civic.db"
    node_id: str = "node-1"

    # Input caps
    max_text_bytes: int = 2 * 1024 * 1024       # 2 MiB
    max_file_bytes: int = 20 * 1024 * 1024      # 20 MiB
    max_text_chars: int = 500_000

    # Timeouts & retries (seconds)
    tool_timeout_seconds: float = 30.0
    health_timeout_seconds: float = 5.0
    sync_timeout_seconds: float = 60.0
    sync_max_retries: int = 3
    sync_backoff_base: float = 0.5
    sync_backoff_cap: float = 8.0
    sqlite_timeout_seconds: float = 30.0

    # Rate limiting
    rate_limit_per_second: float = 100.0
    rate_limit_enabled: bool = True

    # Audit verification cache (avoids O(N) walk on every /health)
    audit_verify_cache_seconds: float = 5.0

    def validate(self) -> None:
        checks = [
            (self.max_text_bytes > 0, "max_text_bytes must be > 0"),
            (self.max_file_bytes > 0, "max_file_bytes must be > 0"),
            (self.tool_timeout_seconds > 0, "tool_timeout_seconds must be > 0"),
            (self.health_timeout_seconds > 0, "health_timeout_seconds must be > 0"),
            (self.sync_timeout_seconds > 0, "sync_timeout_seconds must be > 0"),
            (self.sync_max_retries >= 1, "sync_max_retries must be >= 1"),
            (self.rate_limit_per_second > 0, "rate_limit_per_second must be > 0"),
            (bool(self.db_path), "db_path must not be empty"),
            (bool(self.node_id), "node_id must not be empty"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ConfigError(msg)

    @classmethod
    def from_env(cls, node_id: Optional[str] = None) -> "EngineConfig":
        def _f(name: str, default: float) -> float:
            raw = os.environ.get(name)
            if raw is None:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be a number, got {raw!r}") from exc

        def _i(name: str, default: int) -> int:
            raw = os.environ.get(name)
            if raw is None:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be an int, got {raw!r}") from exc

        cfg = cls(
            db_path=os.environ.get("BHARAT_DB_PATH", "./bharat_civic.db"),
            node_id=node_id or os.environ.get("BHARAT_NODE_ID", "node-1"),
            max_text_bytes=_i("BHARAT_MAX_TEXT_BYTES", 2 * 1024 * 1024),
            max_file_bytes=_i("BHARAT_MAX_FILE_BYTES", 20 * 1024 * 1024),
            max_text_chars=_i("BHARAT_MAX_TEXT_CHARS", 500_000),
            tool_timeout_seconds=_f("BHARAT_TOOL_TIMEOUT_S", 30.0),
            health_timeout_seconds=_f("BHARAT_HEALTH_TIMEOUT_S", 5.0),
            sync_timeout_seconds=_f("BHARAT_SYNC_TIMEOUT_S", 60.0),
            sync_max_retries=_i("BHARAT_SYNC_RETRIES", 3),
            sync_backoff_base=_f("BHARAT_SYNC_BACKOFF_BASE", 0.5),
            sync_backoff_cap=_f("BHARAT_SYNC_BACKOFF_CAP", 8.0),
            rate_limit_per_second=_f("BHARAT_RATE_LIMIT_PER_S", 100.0),
            rate_limit_enabled=os.environ.get("BHARAT_RATE_LIMIT_ENABLED", "1") != "0",
            audit_verify_cache_seconds=_f("BHARAT_AUDIT_CACHE_S", 5.0),
        )
        cfg.validate()
        return cfg


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent JSON-friendly log format with node+request context."""
    root = logging.getLogger()
    if getattr(root, "_bharat_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    setattr(root, "_bharat_configured", True)