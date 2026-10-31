# core/model_expertise.py
"""
AXELR — Model expertise router
================================
Reads ``model_expertise.json`` (written by ``tests.real_evaluator``) and
answers: *which model should this field use?* If the file is missing
or a field's winner is unverified, ``route()`` returns ``None`` and the
caller falls back to the existing router.

Contract:
    * The JSON is re-read only when its mtime changes.
    * ``route()`` is synchronous and cheap (dict lookup).
    * ``route()`` never raises; a corrupt file returns ``None`` for
      every field and logs once.

This is the module that makes the per-field assignment **persistent**.
The existing ``twin_roster.pick_local_model`` heuristic stays in place
as a fallback, so a fresh checkout (no expertise.json) behaves exactly
as before.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("axelr.model_expertise")

_EXPERTISE_PATH = Path(__file__).with_name("model_expertise.json")
_cache_mtime: float = -1.0
_cache: dict[str, dict[str, Any]] | None = None


def _reload_if_changed() -> dict[str, dict[str, Any]] | None:
    global _cache_mtime, _cache
    try:
        st = _EXPERTISE_PATH.stat()
    except FileNotFoundError:
        if _cache is not None:
            logger.info("model_expertise.json removed; falling back to heuristics")
        _cache = None
        _cache_mtime = -1.0
        return None
    if _cache is not None and st.st_mtime == _cache_mtime:
        return _cache
    try:
        raw = json.loads(_EXPERTISE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("model_expertise.json unreadable: %s", e)
        _cache = None
        _cache_mtime = st.st_mtime
        return None
    fields = raw.get("fields") if isinstance(raw, dict) else None
    if not isinstance(fields, dict):
        _cache = None
        _cache_mtime = st.st_mtime
        return None
    _cache = {k: v for k, v in fields.items() if isinstance(v, dict)}
    _cache_mtime = st.st_mtime
    logger.info("model_expertise loaded fields=%d", len(_cache))
    return _cache


def route(field: str) -> str | None:
    """Return the model_id this field is assigned to, or ``None``."""
    fields = _reload_if_changed()
    if not fields:
        return None
    ent = fields.get(field)
    if not ent or not ent.get("verified"):
        return None
    model = ent.get("model")
    return str(model) if model else None


def snapshot() -> dict[str, Any]:
    fields = _reload_if_changed() or {}
    return {
        "path": str(_EXPERTISE_PATH),
        "loaded": _cache is not None,
        "fields": {k: dict(v) for k, v in fields.items()},
    }


def audit() -> dict[str, Any]:
    return snapshot()


__all__ = ["route", "snapshot", "audit"]