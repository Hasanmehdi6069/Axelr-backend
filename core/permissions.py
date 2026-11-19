# core/permissions.py
"""
AXELR — Permission & Consent Management System
Replaces Wispr Flow's permission system with a modern, trust-first approach.
Asks users for critical permissions only when needed, maintains full transparency.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable


def _bool(key: str, default: bool = False) -> bool:
    v = (os.getenv(key, "") or "").lower()
    if not v: return default
    return v in ("1", "true", "yes", "on")

@dataclass(slots=True, frozen=True)
class Permission:
    """Represents a single permission that users can grant/deny."""
    id: str
    name: str
    description: str
    required: bool = False
    category: str = "general"  # privacy, security, functionality, analytics
    default_value: bool = False

# Critical permissions required for core functionality (Wispr flow replacement)
CORE_PERMISSIONS: list[Permission] = [
    Permission(
        id="data_processing",
        name="Basic Data Processing",
        description="Process your prompts and files to provide AI assistance. Required for core functionality.",
        required=True,
        category="privacy",
        default_value=True
    ),
    Permission(
        id="local_inference",
        name="Local Model Execution",
        description="Run lightweight AI models locally on your device to reduce latency and costs.",
        required=False,
        category="functionality",
        default_value=True
    ),
    Permission(
        id="file_analysis",
        name="File Content Analysis",
        description="Analyze uploaded files to provide relevant AI assistance. Only processed when you upload files.",
        required=False,
        category="privacy",
        default_value=True
    ),
    Permission(
        id="telemetry",
        name="Anonymous Usage Analytics",
        description="Share anonymous usage data to help improve Axelr. No personal data is ever collected.",
        required=False,
        category="analytics",
        default_value=False
    ),
    Permission(
        id="error_reporting",
        name="Error Reporting",
        description="Automatically send crash reports and error logs to help us fix issues faster.",
        required=False,
        category="security",
        default_value=True
    )
]

@dataclass(slots=True)
class UserConsent:
    """Stores user's consent choices for all permissions."""
    granted: set[str] = field(default_factory=set)
    denied: set[str] = field(default_factory=set)
    granted_at: dict[str, float] = field(default_factory=dict)

    def has_permission(self, perm_id: str) -> bool:
        """Check if user has granted a specific permission."""
        if perm_id in self.granted:
            return True
        # Check if it's a required permission (auto-granted if required)
        for p in CORE_PERMISSIONS:
            if p.id == perm_id and p.required:
                return True
        return False

    def grant(self, perm_id: str, timestamp: float | None = None) -> None:
        """Grant a permission."""
        if perm_id in self.denied:
            self.denied.remove(perm_id)
        self.granted.add(perm_id)
        if timestamp:
            self.granted_at[perm_id] = timestamp

    def deny(self, perm_id: str) -> None:
        """Deny a permission."""
        if perm_id in self.granted:
            self.granted.remove(perm_id)
        self.denied.add(perm_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialize consent state."""
        return {
            "granted": list(self.granted),
            "denied": list(self.denied),
            "granted_at": self.granted_at
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserConsent":
        """Deserialize consent state."""
        instance = cls()
        instance.granted = set(data.get("granted", []))
        instance.denied = set(data.get("denied", []))
        instance.granted_at = data.get("granted_at", {})
        return instance

def get_required_permissions() -> list[Permission]:
    """Get all required permissions that must be granted."""
    return [p for p in CORE_PERMISSIONS if p.required]

def get_optional_permissions() -> list[Permission]:
    """Get all optional permissions that users can choose."""
    return [p for p in CORE_PERMISSIONS if not p.required]

def check_permissions(consent: UserConsent, required: Iterable[str]) -> tuple[bool, list[str]]:
    """Check if all required permissions are granted."""
    missing = []
    for perm_id in required:
        if not consent.has_permission(perm_id):
            missing.append(perm_id)
    return len(missing) == 0, missing

import json
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple


@dataclass(slots=True)
class ConsentAuditEntry:
    ts: float
    perm_id: str
    action: str   # "granted" | "denied" | "expired"

class ConsentStore:
    """Persists UserConsent + audit log. GDPR-compliant."""
    SCHEMA_VERSION = 1

    def __init__(self, path: Optional[Path] = None) -> None:
        home = Path(os.environ.get("AXELR_HOME", Path.home() / ".axelr"))
        home.mkdir(parents=True, exist_ok=True)
        self._path = path or home / "consent.json"
        self._audit_path = home / "consent_audit.jsonl"
        self._lock = threading.Lock()
        self._consent = self._load()

    def _load(self) -> UserConsent:
        if not self._path.exists():
            return UserConsent()
        try:
            raw = json.loads(self._path.read_text())
            if raw.get("schema_version") != self.SCHEMA_VERSION:
                return UserConsent()
            return UserConsent.from_dict(raw)
        except Exception:
            return UserConsent()

    def save(self) -> None:
        with self._lock:
            self._path.write_text(json.dumps({
                "schema_version": self.SCHEMA_VERSION,
                "consent": self._consent.to_dict(),
            }))

    def grant(self, perm_id: str) -> None:
        self._consent.grant(perm_id, time.time())
        self._append_audit(perm_id, "granted")
        self.save()

    def deny(self, perm_id: str) -> None:
        self._consent.deny(perm_id)
        self._append_audit(perm_id, "denied")
        self.save()

    def _append_audit(self, perm_id: str, action: str) -> None:
        entry = ConsentAuditEntry(time.time(), perm_id, action)
        with self._audit_path.open("a") as f:
            f.write(json.dumps(entry.__dict__) + "\n")

    def check_graceful(self, required: Iterable[str]) -> Tuple[bool, list[str]]:
        """Non-blocking permission check for graceful degradation."""
        return check_permissions(self._consent, required)

    def export_for_gdpr(self) -> Dict[str, Any]:
        """Right-to-access: full consent state + audit trail."""
        audit = []
        if self._audit_path.exists():
            for line in self._audit_path.read_text().splitlines():
                try: audit.append(json.loads(line))
                except: pass
        return {"consent": self._consent.to_dict(), "audit": audit}

    def forget(self) -> None:
        """Right-to-erasure: wipe all consent state."""
        self._consent = UserConsent()
        if self._path.exists(): self._path.unlink()
        if self._audit_path.exists(): self._audit_path.unlink()

__all__ = [
    "Permission",
    "UserConsent",
    "ConsentStore",
    "ConsentAuditEntry",
    "CORE_PERMISSIONS",
    "get_required_permissions",
    "get_optional_permissions",
    "check_permissions"
]
