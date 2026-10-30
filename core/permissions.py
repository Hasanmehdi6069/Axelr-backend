# core/permissions.py
"""
AXELR — Permission & Consent Management System
Replaces Wispr Flow's permission system with a modern, trust-first approach.
Asks users for critical permissions only when needed, maintains full transparency.
"""
from __future__ import annotations
import os
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

__all__ = [
    "Permission",
    "UserConsent",
    "CORE_PERMISSIONS",
    "get_required_permissions",
    "get_optional_permissions",
    "check_permissions"
]