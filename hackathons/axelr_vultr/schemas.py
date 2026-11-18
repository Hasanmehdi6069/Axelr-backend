from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, validator


class InstanceState(str, Enum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DeploymentStrategy(str, Enum):
    LOWEST_COST = "lowest_cost"
    LOWEST_LATENCY = "lowest_latency"
    HIGH_AVAILABILITY = "high_availability"
    GEO_DISTRIBUTED = "geo_distributed"
    CUSTOM = "custom"


class HealthStatus(BaseModel):
    instance_id: str
    status: str
    power_status: str
    is_healthy: bool
    last_checked: datetime
    metrics: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class AutoRecoveryEvent(BaseModel):
    event_id: str
    instance_id: str
    event_type: str
    timestamp: datetime
    details: Dict[str, Any] = Field(default_factory=dict)


class CostOptimizationReport(BaseModel):
    generated_at: datetime
    total_monthly_cost: float
    potential_savings: float
    idle_instances: List[Dict[str, Any]] = Field(default_factory=list)
    overprovisioned_instances: List[Dict[str, Any]] = Field(default_factory=list)
    underutilized_instances: List[Dict[str, Any]] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)


class DeploymentRequest(BaseModel):
    deployment_id: Optional[str] = None
    region: str = "us-east"
    plan: str = "vc2-1c-1gb"
    image: str = "debian-12"
    label: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    ssh_key_ids: List[str] = Field(default_factory=list)
    user_data: Optional[str] = None
    startup_script_id: Optional[str] = None
    enable_ipv6: bool = True
    private_network_ids: List[str] = Field(default_factory=list)
    backups: str = "disabled"
    hostname: Optional[str] = None
    tag: Optional[str] = None
    count: int = Field(default=1, ge=1, le=100)
    strategy: DeploymentStrategy = DeploymentStrategy.LOWEST_COST
    auto_recovery: bool = True
    cost_optimization: bool = True

    @validator("plan")
    def validate_plan(cls, v):
        valid_plans = [
            "vc2-1c-1gb", "vc2-1c-2gb", "vc2-2c-4gb", "vc2-4c-8gb",
            "vc2-8c-16gb", "vc2-16c-32gb", "vc2-32c-64gb",
            "vc3-1c-1gb", "vc3-1c-2gb", "vc3-2c-4gb", "vc3-4c-8gb",
            "vgpu-1a100-80gb", "vgpu-1h100-80gb",
        ]
        if v not in valid_plans and not v.startswith("custom-"):
            raise ValueError(f"Invalid plan: {v}. Must be one of {valid_plans}")
        return v

    @validator("region")
    def validate_region(cls, v):
        valid_regions = [
            "us-east", "us-west", "us-south", "us-central",
            "eu-west", "eu-central", "eu-north",
            "ap-southeast", "ap-northeast", "ap-south",
            "au", "in", "sg", "jp", "kr",
        ]
        if v not in valid_regions and not v.startswith("custom-"):
            raise ValueError(f"Invalid region: {v}. Must be one of {valid_regions}")
        return v


class DeploymentStatus(BaseModel):
    deployment_id: str
    request: DeploymentRequest
    state: InstanceState
    instance_id: Optional[str] = None
    created_at: datetime
    provisioned_at: Optional[datetime] = None
    ready_at: Optional[datetime] = None
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        end_time = self.ready_at or datetime.utcnow()
        return (end_time - self.created_at).total_seconds()

    @property
    def is_complete(self) -> bool:
        return self.state in [InstanceState.RUNNING, InstanceState.FAILED, InstanceState.TERMINATED]


class InstanceMetrics(BaseModel):
    instance_id: str
    timestamp: datetime
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    disk_usage: float = 0.0
    network_in: float = 0.0
    network_out: float = 0.0
    uptime_seconds: float = 0.0


class VultrAPIStatus(BaseModel):
    status: str
    latency_ms: float
    timestamp: datetime
    endpoints: Dict[str, Any] = Field(default_factory=dict)
    rate_limit_remaining: Optional[int] = None
    rate_limit_reset: Optional[int] = None


class SystemHealthMetrics(BaseModel):
    version: str = "3.0.0"
    status: str
    uptime_seconds: float
    requests_total: int
    requests_success: int
    requests_failed: int
    average_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    cache_hit_rate: float
    rate_limiter_capacity: int
    circuit_breaker_state: str
    active_deployments: int
    total_instances: int
    healthy_instances: int
    cost_estimate_monthly: float
    potential_savings: float


class AuditLogEntry(BaseModel):
    log_id: str
    timestamp: datetime
    event_type: str
    user_id: Optional[str] = None
    request_id: Optional[str] = None
    instance_id: Optional[str] = None
    deployment_id: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    severity: str = "info"
    success: bool = True


class BatchOperationRequest(BaseModel):
    operations: List[Dict[str, Any]] = Field(..., min_items=1, max_items=100)
    parallel: bool = True
    continue_on_error: bool = False


class BatchOperationResult(BaseModel):
    batch_id: str
    total_operations: int
    successful: int
    failed: int
    results: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None


class RegionCapacityInfo(BaseModel):
    region: str
    plan: str
    available: bool
    last_checked: datetime
    estimated_provision_time_seconds: float
    cost_multiplier: float = 1.0


class RecommendationItem(BaseModel):
    type: str
    title: str
    description: str
    priority: str = "medium"
    estimated_savings: Optional[float] = None
    estimated_performance_improvement: Optional[float] = None
    action_url: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class OptimizationRecommendations(BaseModel):
    generated_at: datetime
    total_recommendations: int
    high_priority: List[RecommendationItem] = Field(default_factory=list)
    medium_priority: List[RecommendationItem] = Field(default_factory=list)
    low_priority: List[RecommendationItem] = Field(default_factory=list)
    total_potential_savings: float = 0.0
    total_performance_improvement: float = 0.0


class InstanceConfiguration(BaseModel):
    instance_id: str
    region: str
    plan: str
    image: str
    tags: List[str] = Field(default_factory=list)
    user_data: Optional[str] = None
    ssh_keys: List[str] = Field(default_factory=list)
    firewall_group_id: Optional[str] = None
    private_network_ids: List[str] = Field(default_factory=list)
    enable_ipv6: bool = True
    backups_enabled: bool = False
    label: Optional[str] = None
    hostname: Optional[str] = None


class DeploymentBlueprint(BaseModel):
    blueprint_id: str
    name: str
    description: str
    category: str
    version: str = "1.0.0"
    configuration: InstanceConfiguration
    estimated_cost_monthly: float
    estimated_deployment_time_seconds: float
    requirements: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ValidationResult(BaseModel):
    is_valid: bool
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class FeatureFlagsConfig(BaseModel):
    adaptive_scaling_enabled: bool = True
    auto_recovery_enabled: bool = True
    observability_enabled: bool = True
    cost_optimization_enabled: bool = True
    security_scan_enabled: bool = True
    ai_optimization_enabled: bool = True
    edge_acceleration_enabled: bool = True
    zero_trust_enabled: bool = True
    http2_enabled: bool = True
    batch_operations_enabled: bool = True
    predictive_provisioning_enabled: bool = True
    anomaly_detection_enabled: bool = True


class RateLimitConfig(BaseModel):
    requests_per_minute: int = 600
    burst_size: int = 100
    adaptive_capacity: bool = True
    min_capacity: int = 10
    max_capacity: int = 200
    success_rate_threshold: float = 0.95


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 5
    recovery_timeout_seconds: int = 30
    max_recovery_attempts: int = 3
    predictive_recovery: bool = True
    half_open_max_requests: int = 5


class CacheConfig(BaseModel):
    default_ttl_seconds: int = 60
    max_size: int = 10000
    lru_eviction_enabled: bool = True
    cache_warming_enabled: bool = True
    cache_invalidation_enabled: bool = True


class RetryConfig(BaseModel):
    max_attempts: int = 5
    backoff_base: float = 1.0
    max_backoff_seconds: int = 30.0
    jitter_enabled: bool = True
    retry_on_timeout: bool = True
    retry_on_server_error: bool = True
    retry_on_rate_limit: bool = True


class EngineConfig(BaseModel):
    version: str = "3.0.0"
    feature_flags: FeatureFlagsConfig = Field(default_factory=FeatureFlagsConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    api_timeout_seconds: int = 30
    max_concurrent_connections: int = 100
    health_check_interval_seconds: int = 30
    cost_optimization_interval_hours: int = 24
    auto_recovery_interval_seconds: int = 60
    observability_retention_days: int = 30
    audit_log_retention_days: int = 90
    created_at: datetime
    updated_at: datetime


class BulkProvisionRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=100)
    region: str = "us-east"
    plan: str = "vc2-1c-1gb"
    image: str = "debian-12"
    tags: List[str] = Field(default_factory=list)


class CleanupRequest(BaseModel):
    dry_run: bool = False
    older_than_days: int = 30


class CreateInstanceRequest(BaseModel):
    region: str = "us-east"
    plan: str = "vc2-1c-1gb"
    image: str = "debian-12"
    label: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class UpdateInstanceRequest(BaseModel):
    instance_id: str
    label: Optional[str] = None
    tags: Optional[List[str]] = None


class FirewallGroupCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    region: str = "us-east"


class FirewallRuleRequest(BaseModel):
    group_id: str
    direction: str = "in"
    protocol: str = "tcp"
    port: str = "22"
    source: str = "0.0.0.0/0"


class SSHKeyCreateRequest(BaseModel):
    name: str
    ssh_key: str


class SnapshotCreateRequest(BaseModel):
    instance_id: str
    description: Optional[str] = None


class EliteDeploymentRequest(BaseModel):
    blueprint_id: str
    region: str = "us-east"
    count: int = Field(default=1, ge=1, le=10)


class EliteDeploymentStatus(BaseModel):
    deployment_id: str
    state: str = "pending"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SystemHealthMetrics(BaseModel):
    healthy: bool = True
    total_requests: int = 0
    success_rate: float = 1.0
    average_latency_ms: float = 0.0
    active_instances: int = 0


class InstanceResponse(BaseModel):
    id: str
    region: str
    plan: str
    status: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BlueprintResponse(BaseModel):
    id: str
    name: str
    description: str
    cost_estimate: float = 0.0


class RegionResponse(BaseModel):
    id: str
    name: str
    available: bool = True


class PlanResponse(BaseModel):
    id: str
    name: str
    vcpus: int = 1
    memory_gb: int = 1
    cost_per_month: float = 0.0


class CostEstimate(BaseModel):
    total_cost: float = 0.0
    per_instance: float = 0.0
    monthly_total: float = 0.0


class OrphanInstance(BaseModel):
    id: str
    region: str
    created_at: datetime
    age_days: float = 0.0


class SnapshotResponse(BaseModel):
    id: str
    instance_id: str
    description: str
    size_gb: float = 0.0
    created_at: datetime


class SSHKeyResponse(BaseModel):
    id: str
    name: str
    fingerprint: str
    created_at: datetime


class FirewallGroupResponse(BaseModel):
    id: str
    name: str
    region: str
    rule_count: int = 0


class BandwidthResponse(BaseModel):
    instance_id: str
    used_gb: float = 0.0
    limit_gb: float = 0.0
    month: str