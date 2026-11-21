"""axelr-insights-engine — Pydantic models + all standard schemas.

Merged from models.py + setup.py schema constants. Every table shape
the engine ships with is declared here — no duplicate definitions
across files.
"""

from __future__ import annotations

from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FilterOp = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "starts_with",
    "ends_with",
    "contains",
    "search",
    "is_null",
    "is_not_null",
    "between",
]
AttributeType = Literal["string", "integer", "double", "boolean", "datetime"]
IndexType = Literal["key", "unique", "fulltext"]


# ═══════════════════════════════════════════════════════════════════════
# Query models
# ═══════════════════════════════════════════════════════════════════════


class QueryFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(..., min_length=1, max_length=255)
    op: FilterOp = "eq"
    value: Any = None

    @field_validator("field")
    @classmethod
    def _field_clean(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("field cannot be blank")
        return v

    def to_tuple(self) -> tuple[str, str, Any]:
        return (self.field, self.op, self.value)

    @classmethod
    def eq(cls, f: str, v: Any) -> "QueryFilter":
        return cls(field=f, op="eq", value=v)

    @classmethod
    def ne(cls, f: str, v: Any) -> "QueryFilter":
        return cls(field=f, op="ne", value=v)

    @classmethod
    def gt(cls, f: str, v: Any) -> "QueryFilter":
        return cls(field=f, op="gt", value=v)

    @classmethod
    def gte(cls, f: str, v: Any) -> "QueryFilter":
        return cls(field=f, op="gte", value=v)

    @classmethod
    def lt(cls, f: str, v: Any) -> "QueryFilter":
        return cls(field=f, op="lt", value=v)

    @classmethod
    def lte(cls, f: str, v: Any) -> "QueryFilter":
        return cls(field=f, op="lte", value=v)

    @classmethod
    def in_(cls, f: str, vs: list[Any]) -> "QueryFilter":
        return cls(field=f, op="in", value=vs)

    @classmethod
    def contains(cls, f: str, v: Any) -> "QueryFilter":
        return cls(field=f, op="contains", value=v)

    @classmethod
    def starts_with(cls, f: str, v: str) -> "QueryFilter":
        return cls(field=f, op="starts_with", value=v)

    @classmethod
    def between(cls, f: str, lo: Any, hi: Any) -> "QueryFilter":
        return cls(field=f, op="between", value=[lo, hi])


class QueryOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: list[QueryFilter] = Field(default_factory=list)
    order_by: str | None = None
    order_desc: bool = True
    limit: int | None = Field(default=None, ge=1, le=5000)
    offset: int | None = Field(default=None, ge=0)
    cursor: str | None = None
    select: list[str] | None = None
    include_deleted: bool = False
    cacheable: bool = True

    @classmethod
    def of(cls, *filters: QueryFilter, **kw: Any) -> "QueryOptions":
        return cls(filters=list(filters), **kw)

    def merged_filters(self, extra: list[QueryFilter] | None = None) -> list[QueryFilter]:
        return list(self.filters) + list(extra or [])

    def with_user(self, user_id: str) -> "QueryOptions":
        """Return a copy scoped to a specific user."""
        return self.model_copy(
            update={
                "filters": [QueryFilter.eq("user_id", user_id), *self.filters],
            }
        )


class QueryBuilder:
    """Fluent builder for common query patterns."""

    def __init__(self) -> None:
        self._filters: list[QueryFilter] = []
        self._order: str | None = None
        self._desc = True
        self._limit: int | None = None
        self._offset: int | None = None

    def where(self, field: str, op: FilterOp, value: Any) -> "QueryBuilder":
        self._filters.append(QueryFilter(field=field, op=op, value=value))
        return self

    def where_eq(self, field: str, value: Any) -> "QueryBuilder":
        return self.where(field, "eq", value)

    def where_in(self, field: str, values: list[Any]) -> "QueryBuilder":
        return self.where(field, "in", values)

    def since(self, field: str, ts: Any) -> "QueryBuilder":
        return self.where(field, "gte", ts)

    def until(self, field: str, ts: Any) -> "QueryBuilder":
        return self.where(field, "lte", ts)

    def order_by(self, field: str, desc: bool = True) -> "QueryBuilder":
        self._order = field
        self._desc = desc
        return self

    def limit(self, n: int) -> "QueryBuilder":
        self._limit = n
        return self

    def offset(self, n: int) -> "QueryBuilder":
        self._offset = n
        return self

    def build(self) -> QueryOptions:
        return QueryOptions(
            filters=self._filters,
            order_by=self._order,
            order_desc=self._desc,
            limit=self._limit,
            offset=self._offset,
        )


# ═══════════════════════════════════════════════════════════════════════
# Schema models
# ═══════════════════════════════════════════════════════════════════════


class AttributeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    type: AttributeType
    size: int | None = Field(default=None, ge=1, le=1_048_576)
    required: bool = False
    default: Any = None
    array: bool = False

    @field_validator("size")
    @classmethod
    def _size(cls, v: int | None, info: Any) -> int | None:
        if info.data.get("type") == "string" and v is None:
            return 255
        return v


class IndexSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(..., min_length=1, max_length=64)
    type: IndexType = "key"
    attributes: list[str] = Field(..., min_length=1)
    orders: list[Literal["ASC", "DESC"]] | None = None

    def resolved_orders(self) -> list[str]:
        return list(self.orders) if self.orders else ["ASC"] * len(self.attributes)


class SchemaDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    name: str | None = None
    attributes: list[AttributeSpec] = Field(default_factory=list)
    indexes: list[IndexSpec] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)

    def resolved_name(self) -> str:
        return self.name or self.table_id

    def attribute_keys(self) -> list[str]:
        return [a.key for a in self.attributes]

    def index_keys(self) -> list[str]:
        return [i.key for i in self.indexes]


def schema_from_dict(data: dict[str, Any]) -> SchemaDefinition:
    """Build a SchemaDefinition from a plain dict."""
    return SchemaDefinition(**data)


def merge_schemas(*schemas: SchemaDefinition) -> SchemaDefinition:
    """Merge multiple schema fragments into one.

    Later schemas win on attribute/index key conflicts.
    """
    if not schemas:
        raise ValueError("at least one schema required")
    base = schemas[0]
    attrs_by_key: dict[str, AttributeSpec] = {a.key: a for a in base.attributes}
    idxs_by_key: dict[str, IndexSpec] = {i.key: i for i in base.indexes}
    for s in schemas[1:]:
        for a in s.attributes:
            attrs_by_key[a.key] = a
        for i in s.indexes:
            idxs_by_key[i.key] = i
    return SchemaDefinition(
        table_id=base.table_id,
        name=base.name,
        attributes=list(attrs_by_key.values()),
        indexes=list(idxs_by_key.values()),
        permissions=base.permissions,
    )


def extend_schema(
    base: SchemaDefinition, *, attributes: list[AttributeSpec] | None = None, indexes: list[IndexSpec] | None = None
) -> SchemaDefinition:
    """Add attributes/indexes to an existing schema definition."""
    existing_attrs = {a.key: a for a in base.attributes}
    for a in attributes or []:
        existing_attrs[a.key] = a
    existing_idx = {i.key: i for i in base.indexes}
    for i in indexes or []:
        existing_idx[i.key] = i
    return SchemaDefinition(
        table_id=base.table_id,
        name=base.name,
        attributes=list(existing_attrs.values()),
        indexes=list(existing_idx.values()),
        permissions=base.permissions,
    )


# ═══════════════════════════════════════════════════════════════════════
# Standard schemas (moved from setup.py)
# ═══════════════════════════════════════════════════════════════════════

SCHEMA_AUDIT_EVENTS = SchemaDefinition(
    table_id="audit_events",
    name="audit_events",
    attributes=[
        AttributeSpec(key="kind", type="string", size=64, required=True),
        AttributeSpec(key="payload", type="string", size=65535),
        AttributeSpec(key="ts", type="datetime", required=True),
        AttributeSpec(key="user_id", type="string", size=255),
        AttributeSpec(key="pipeline_id", type="string", size=255),
        AttributeSpec(key="status", type="string", size=32),
        AttributeSpec(key="latency_ms", type="double"),
        AttributeSpec(key="day", type="string", size=10),
        AttributeSpec(key="_version", type="integer"),
        AttributeSpec(key="_deleted", type="boolean"),
    ],
    indexes=[
        IndexSpec(key="kind_ts", type="key", attributes=["kind", "ts"], orders=["ASC", "DESC"]),
        IndexSpec(key="ts_desc", type="key", attributes=["ts"], orders=["DESC"]),
        IndexSpec(key="pipeline_ts", type="key", attributes=["pipeline_id", "ts"], orders=["ASC", "DESC"]),
        IndexSpec(key="day_kind", type="key", attributes=["day", "kind"]),
    ],
)

SCHEMA_INDEXED_DOCUMENTS = SchemaDefinition(
    table_id="indexed_documents",
    name="indexed_documents",
    attributes=[
        AttributeSpec(key="doc_id", type="string", size=255, required=True),
        AttributeSpec(key="content", type="string", size=1_048_576, required=True),
        AttributeSpec(key="meta", type="string", size=65535),
        AttributeSpec(key="ts", type="datetime", required=True),
        AttributeSpec(key="_version", type="integer"),
        AttributeSpec(key="_deleted", type="boolean"),
    ],
    indexes=[
        IndexSpec(key="doc_id_unique", type="unique", attributes=["doc_id"]),
        IndexSpec(key="ts_desc", type="key", attributes=["ts"], orders=["DESC"]),
    ],
)

SCHEMA_AXELR_INSIGHTS = SchemaDefinition(
    table_id="axelr_insights",
    name="axelr_insights",
    attributes=[
        AttributeSpec(key="user_id", type="string", size=255, required=True),
        AttributeSpec(key="insight_type", type="string", size=100, required=True),
        AttributeSpec(key="content", type="string", size=1_048_576, required=True),
        AttributeSpec(key="timestamp", type="integer", required=True),
        AttributeSpec(key="metadata", type="string", size=65535),
        AttributeSpec(key="confidence_score", type="double"),
        AttributeSpec(key="system_origin", type="string", size=50, required=True),
        AttributeSpec(key="_version", type="integer"),
        AttributeSpec(key="_deleted", type="boolean"),
    ],
    indexes=[
        IndexSpec(key="user_origin", type="key", attributes=["user_id", "system_origin"]),
        IndexSpec(key="type_origin", type="key", attributes=["insight_type", "system_origin"]),
        IndexSpec(key="ts_desc", type="key", attributes=["timestamp"], orders=["DESC"]),
    ],
)

ALL_SCHEMAS: list[SchemaDefinition] = [SCHEMA_AUDIT_EVENTS, SCHEMA_INDEXED_DOCUMENTS, SCHEMA_AXELR_INSIGHTS]

SCHEMAS_BY_ID: dict[str, SchemaDefinition] = {s.table_id: s for s in ALL_SCHEMAS}


# ═══════════════════════════════════════════════════════════════════════
# Row shapes
# ═══════════════════════════════════════════════════════════════════════


class BaseRow(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @property
    def id(self) -> str:
        return getattr(self, "$id", "") or ""

    @property
    def created_at(self) -> str:
        return getattr(self, "$createdAt", "") or ""

    @property
    def updated_at(self) -> str:
        return getattr(self, "$updatedAt", "") or ""

    @property
    def is_deleted(self) -> bool:
        return bool(getattr(self, "_deleted", False))


class AuditEventRow(BaseRow):
    kind: str = Field(..., max_length=64)
    ts: str
    payload: str | None = Field(default=None, max_length=65535)
    user_id: str | None = Field(default=None, max_length=255)
    pipeline_id: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, max_length=32)
    latency_ms: float | None = Field(default=None, ge=0)
    day: str | None = Field(default=None, max_length=10)
    _version: int | None = Field(default=None, alias="_version")
    _deleted: bool | None = Field(default=None, alias="_deleted")


class InsightRow(BaseRow):
    user_id: str = Field(..., max_length=255)
    insight_type: str = Field(..., max_length=100)
    content: str = Field(..., max_length=1_048_576)
    timestamp: int
    metadata: str | None = Field(default=None, max_length=65535)
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    system_origin: str = Field(..., max_length=50)
    _version: int | None = Field(default=None, alias="_version")
    _deleted: bool | None = Field(default=None, alias="_deleted")


class DocumentRow(BaseRow):
    doc_id: str = Field(..., max_length=255)
    content: str = Field(..., max_length=1_048_576)
    meta: str | None = Field(default=None, max_length=65535)
    ts: str


class NotificationRow(BaseRow):
    """A notification delivered to a user."""

    user_id: str = Field(..., max_length=255)
    title: str = Field(..., max_length=255)
    body: str = Field(..., max_length=65535)
    read: bool = False
    created_ts: int
    _version: int | None = Field(default=None, alias="_version")
    _deleted: bool | None = Field(default=None, alias="_deleted")


class MetricRow(BaseRow):
    """A named metric data point."""

    name: str = Field(..., max_length=100)
    value: float
    dimensions: str | None = Field(default=None, max_length=65535)
    ts: str


class PipelineStateRow(BaseRow):
    """Snapshot of a pipeline's state at a point in time."""

    pipeline_id: str = Field(..., max_length=255)
    state: str = Field(..., max_length=1_048_576)
    ts: str


class PaymentRow(BaseRow):
    """A payment record (used by the PayPal agent)."""

    order_id: str = Field(..., max_length=255)
    user_id: str = Field(..., max_length=255)
    amount_cents: int = Field(..., ge=0)
    currency: str = Field(..., min_length=3, max_length=3)
    status: str = Field(..., max_length=32)
    created_ts: int
    _version: int | None = Field(default=None, alias="_version")
    _deleted: bool | None = Field(default=None, alias="_deleted")


ROW_MODELS: dict[str, type[BaseRow]] = {
    "audit_events": AuditEventRow,
    "indexed_documents": DocumentRow,
    "axelr_insights": InsightRow,
    "notifications": NotificationRow,
    "metrics": MetricRow,
    "pipeline_state": PipelineStateRow,
    "payments": PaymentRow,
}


def coerce_row(table: str, data: dict[str, Any]) -> BaseRow:
    """Parse a raw row dict into its typed model (falls back to BaseRow)."""
    model = ROW_MODELS.get(table)
    if model is None:
        return BaseRow(**data)
    return model(**data)


def validate_row(table: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validate a row against its schema before write. Raises ValidationFailed."""
    from .errors import ValidationFailed

    model = ROW_MODELS.get(table)
    if model is None:
        return data
    try:
        model(**data)
    except Exception as e:
        raise ValidationFailed(f"row for {table!r} failed schema: {e}") from e
    return data


# ═══════════════════════════════════════════════════════════════════════
# Report models
# ═══════════════════════════════════════════════════════════════════════


class SchemaReport(BaseModel):
    table_id: str
    table_exists: bool
    missing_attributes: list[str] = Field(default_factory=list)
    missing_indexes: list[str] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.table_exists and not self.missing_attributes and not self.missing_indexes


class HealthReport(BaseModel):
    model_config = ConfigDict(extra="allow")
    healthy: bool
    backend: str = "appwrite"
    mode: str
    database_id: str
    circuit_state: str
    cache_size: int
    initialized: bool
    timestamp: float
    reason: str | None = None
    schema: dict[str, Any] | None = None
    schema_ok: bool | None = None
    metrics: dict[str, Any] | None = None


def iter_schema_definitions() -> Iterator[SchemaDefinition]:
    """Iterate over all standard schemas."""
    yield from ALL_SCHEMAS
