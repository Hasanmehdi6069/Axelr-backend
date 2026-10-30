# axelr_vultr/schemas.py
"""Pydantic v2 schemas — strict, extra-forbidden, fully validated."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BlueprintName = Literal[
    "inference_worker", "sandbox", "batch_processor",
    "web_node", "custom",
]

InstanceStatus = Literal[
    "pending", "active", "stopped", "suspended",
    "resizing", "reinstalling", "restoring", "unknown",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateInstanceRequest(_Strict):
    blueprint: BlueprintName = "inference_worker"
    region: str | None = Field(default=None, max_length=16, pattern=r"^[a-z0-9\-]+$")
    plan: str | None = Field(default=None, max_length=32, pattern=r"^[a-z0-9\-]+$")
    label: str | None = Field(default=None, min_length=1, max_length=64,
                              pattern=r"^[A-Za-z0-9\-_\.]+$")
    os_id: int | None = Field(default=None, ge=1, le=10000)
    startup_script: str | None = Field(default=None, max_length=65535)
    ssh_key_ids: list[str] = Field(default_factory=list, max_length=5)
    tags: list[str] = Field(default_factory=list, max_length=10)
    dry_run: bool = False
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for t in v:
            t = t.strip()
            if not t:
                continue
            if not t.replace("-", "").replace("_", "").replace(".", "").isalnum():
                raise ValueError(f"invalid tag: {t!r}")
            out.append(t[:32])
        return out


class InstanceResponse(_Strict):
    id: str
    label: str
    region: str
    plan: str
    status: InstanceStatus = "pending"
    main_ip: str | None = None
    v6_main_ip: str | None = None
    os: str | None = None
    ram_mb: int | None = None
    vcpu_count: int | None = None
    disk_gb: int | None = None
    monthly_cost_usd: float | None = None
    created_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    is_axelr_managed: bool = False
    blueprint: str | None = None


class BlueprintResponse(_Strict):
    name: str
    description: str
    plan: str
    region_default: str
    os_id: int
    estimated_monthly_usd: float
    tags: list[str] = Field(default_factory=list)
    hardware_hint: str = ""


class RegionResponse(_Strict):
    id: str
    city: str
    country: str
    continent: str
    options: list[str] = Field(default_factory=list)


class PlanResponse(_Strict):
    id: str
    vcpu_count: int
    ram_mb: int
    disk_gb: int
    monthly_cost_usd: float
    hourly_cost_usd: float
    type: str = "vc2"
    locations: list[str] = Field(default_factory=list)


class CostEstimate(_Strict):
    plan_id: str
    hours: float
    hourly_cost_usd: float
    monthly_cost_usd: float
    estimated_usd: float
    over_budget: bool
    monthly_budget_usd: float
    remaining_budget_usd: float


class CleanupRequest(_Strict):
    dry_run: bool = False
    confirm: bool = False
    max_age_hours: float = Field(default=24.0, ge=0.1, le=720)


class OrphanInstance(_Strict):
    id: str
    label: str
    region: str
    plan: str
    status: InstanceStatus
    created_at: str | None = None
    age_hours: float
    tags: list[str] = Field(default_factory=list)
    estimated_cost_usd: float = 0.0


class StructuredError(BaseModel):
    success: Literal[False] = False
    error: dict[str, Any]