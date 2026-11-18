"""axelr-insights-engine — Schema bootstrap.

Idempotent table/attribute/index creation. All schema definitions live
in models.py — this module only *applies* them.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from .models import (ALL_SCHEMAS, AttributeSpec, IndexSpec, SchemaDefinition,
                     SCHEMAS_BY_ID)
from .sdk import AppwriteSDK

_log = logging.getLogger("axelr_insights_engine.setup")


async def ensure_table(sdk: AppwriteSDK, schema: SchemaDefinition | dict[str, Any],
                       *, verify_only: bool = False) -> dict[str, Any]:
    if not isinstance(schema, SchemaDefinition): schema = SchemaDefinition(**schema)
    report: dict[str, Any] = {"table_id": schema.table_id,
                              "created": [], "skipped": [], "errors": []}
    db_id = sdk.config.database_id

    tables = await _list_tables(sdk)
    if schema.table_id in tables:
        report["skipped"].append(f"table:{schema.table_id}")
    elif verify_only:
        report["errors"].append({"step": "table", "reason": "missing"})
        return report
    else:
        try:
            await sdk.call("schema.create_table",
                           lambda: sdk.tables.create_table(
                               database_id=db_id, table_id=schema.table_id,
                               name=schema.resolved_name(), permissions=[]))
            report["created"].append(f"table:{schema.table_id}")
        except Exception as e:
            msg = str(e).lower()
            if "already exists" in msg or "409" in msg:
                report["skipped"].append(f"table:{schema.table_id}")
            else:
                report["errors"].append({"step": "table", "error": str(e)[:200]})
                return report

    existing_attrs = await _list_attributes(sdk, schema.table_id)
    for attr in schema.attributes:
        if attr.key in existing_attrs:
            report["skipped"].append(f"attr:{schema.table_id}.{attr.key}")
            continue
        if verify_only:
            report["errors"].append({"step": "attr", "key": attr.key, "reason": "missing"})
            continue
        if await _create_attribute(sdk, schema.table_id, attr):
            report["created"].append(f"attr:{schema.table_id}.{attr.key}")
        else:
            report["errors"].append({"step": "attr", "key": attr.key})

    existing_idx = await _list_indexes(sdk, schema.table_id)
    for idx in schema.indexes:
        if idx.key in existing_idx:
            report["skipped"].append(f"idx:{schema.table_id}.{idx.key}")
            continue
        if verify_only:
            report["errors"].append({"step": "idx", "key": idx.key, "reason": "missing"})
            continue
        if await _create_index(sdk, schema.table_id, idx):
            report["created"].append(f"idx:{schema.table_id}.{idx.key}")
        else:
            report["errors"].append({"step": "idx", "key": idx.key})
    return report


async def ensure_all_tables(sdk: AppwriteSDK, *,
                            verify_only: bool = False) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for schema in ALL_SCHEMAS:
        results[schema.table_id] = await ensure_table(sdk, schema, verify_only=verify_only)
    return results


async def verify_table(sdk: AppwriteSDK, schema: SchemaDefinition) -> dict[str, Any]:
    out: dict[str, Any] = {"table_id": schema.table_id, "table_exists": False,
                           "missing_attributes": [], "missing_indexes": []}
    if schema.table_id not in await _list_tables(sdk): return out
    out["table_exists"] = True
    attrs = await _list_attributes(sdk, schema.table_id)
    out["missing_attributes"] = [a.key for a in schema.attributes if a.key not in attrs]
    idxs = await _list_indexes(sdk, schema.table_id)
    out["missing_indexes"] = [i.key for i in schema.indexes if i.key not in idxs]
    return out


async def verify_all_tables(sdk: AppwriteSDK) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for schema in ALL_SCHEMAS:
        results[schema.table_id] = await verify_table(sdk, schema)
    return results


# ── Internal helpers ────────────────────────────────────────────────

async def _list_tables(sdk: AppwriteSDK) -> set[str]:
    try:
        res = await sdk.call("schema.list_tables",
                             lambda: sdk.tables.list_tables(database_id=sdk.config.database_id),
                             bypass_circuit=True)
        raw = res.get("tables") if isinstance(res, dict) else getattr(res, "tables", [])
        return {_id_of(t) for t in (raw or []) if _id_of(t)}
    except Exception:
        return set()


async def _list_attributes(sdk: AppwriteSDK, table_id: str) -> set[str]:
    try:
        res = await sdk.call("schema.list_attrs",
                             lambda: sdk.tables.list_attributes(
                                 database_id=sdk.config.database_id, table_id=table_id),
                             bypass_circuit=True)
        raw = res.get("attributes") if isinstance(res, dict) else getattr(res, "attributes", [])
        return {a.get("key", "") for a in (raw or []) if isinstance(a, dict)}
    except Exception:
        return set()


async def _list_indexes(sdk: AppwriteSDK, table_id: str) -> set[str]:
    try:
        res = await sdk.call("schema.list_idx",
                             lambda: sdk.tables.list_indexes(
                                 database_id=sdk.config.database_id, table_id=table_id),
                             bypass_circuit=True)
        raw = res.get("indexes") if isinstance(res, dict) else getattr(res, "indexes", [])
        return {i.get("key", "") for i in (raw or []) if isinstance(i, dict)}
    except Exception:
        return set()


async def _create_attribute(sdk: AppwriteSDK, table_id: str, attr: AttributeSpec) -> bool:
    db_id = sdk.config.database_id
    payload = {"database_id": db_id, "table_id": table_id,
               "key": attr.key, "required": attr.required}
    if attr.size is not None: payload["size"] = attr.size
    if attr.default is not None: payload["default"] = attr.default
    methods = {"string": "create_string_attribute", "integer": "create_integer_attribute",
               "double": "create_double_attribute", "boolean": "create_boolean_attribute",
               "datetime": "create_datetime_attribute"}
    method = methods.get(attr.type)
    if not method:
        _log.error("unknown attribute type: %s", attr.type)
        return False
    try:
        await sdk.call(f"schema.create_attr.{attr.key}",
                       lambda: getattr(sdk.tables, method)(**payload))
        return True
    except Exception as e:
        msg = str(e).lower()
        if "already exists" in msg or "409" in msg: return True
        _log.warning("create_attr %s.%s failed: %s", table_id, attr.key, str(e)[:160])
        return False


async def _create_index(sdk: AppwriteSDK, table_id: str, idx: IndexSpec) -> bool:
    try:
        await sdk.call(f"schema.create_idx.{idx.key}",
                       lambda: sdk.tables.create_index(
                           database_id=sdk.config.database_id, table_id=table_id,
                           key=idx.key, type=idx.type, attributes=idx.attributes,
                           orders=idx.resolved_orders()))
        return True
    except Exception as e:
        msg = str(e).lower()
        if "already exists" in msg or "409" in msg: return True
        _log.warning("create_idx %s.%s failed: %s", table_id, idx.key, str(e)[:160])
        return False


def _id_of(t: Any) -> str:
    if isinstance(t, dict): return str(t.get("$id") or t.get("id") or "")
    return str(getattr(t, "$id", "") or getattr(t, "id", "") or "")


# ── CLI ────────────────────────────────────────────────────────────

def main() -> int:
    import asyncio
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")

    async def _run() -> int:
        from .config import AppwriteConfig
        from .sdk import AppwriteSDK
        try: config = AppwriteConfig.from_env()
        except Exception as e:
            print(f"✗ Config error: {e}", file=sys.stderr); return 1
        sdk = AppwriteSDK(config)
        try:
            reports = await ensure_all_tables(sdk)
            for tid, rep in reports.items():
                mark = "✓" if not rep["errors"] else "✗"
                print(f"  {mark} {tid:<24} created={len(rep['created'])} "
                      f"skipped={len(rep['skipped'])} errors={len(rep['errors'])}")
                for err in rep["errors"]: print(f"      ! {err}", file=sys.stderr)
            total = sum(len(r["errors"]) for r in reports.values())
            return 0 if total == 0 else 1
        finally:
            await sdk.close()
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())