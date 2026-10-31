<<<<<<< HEAD
# core/structured_gen.py
"""
AXELR — Structured Generation Layer (routing · grammar · verification)
========================================================================
Three primitives that turn a general-purpose LLM call into a *verifiable*
structured-output call. Previously split across `task_router_v2.py`,
`grammar_engine.py`, and `local_verifier.py`; merged because a change to
what "json_schema-verifiable" *means* must touch exactly one file.

    §1  Task Router      classify(prompt, ...) -> RouteDecision
    §2  Grammar Engine   compile_schema(schema) -> GBNF path
    §3  Local Verifier   verify(text, kind=...) -> Verdict

Every function is deterministic, bounded, and never raises. Consumed by
`core.orchestration.hedge_wrap` and `api._boot._install_local_router`.

Env gates
---------
  AXELR_LOCAL_ROUTER  = off | shadow | on   (default off)
  AXELR_LOCAL_VERIFY  = off | shadow | on   (default off)
  AXELR_GRAMMAR_DIR   = ./grammars
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("axelr.structured_gen")

__all__ = [
    # §1 Router
    "classify", "RouteDecision", "TASK_CLASSES",
    # §2 Grammar
    "compile_schema", "get_grammar", "grammar_file_for",
    "GRAMMAR_DIR", "GRAMMAR_CACHE_DIR",
    # §3 Verifier
    "verify", "Verdict", "VerifierKind",
]


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Task Router
# ═══════════════════════════════════════════════════════════════════════════
_MODE = os.getenv("AXELR_LOCAL_ROUTER", "off").strip().lower()
if _MODE not in ("off", "shadow", "on"):
    _MODE = "off"

# (task_class, pattern, verifier_kind, grammar_id). First match wins.
_CLASS_RULES: tuple[tuple[str, re.Pattern[str], str, str | None], ...] = (
    ("pii_scan",     re.compile(r"\b(pii|personally identifiable|redact|anonymi[sz]e)\b", re.I), "json_schema", "pii_spans"),
    ("tool_call",    re.compile(r"\b(function[- ]?call|tool[- ]?call|call the \w+ tool)\b", re.I), "json_schema", "tool_call"),
    ("classify",     re.compile(r"\b(classify|categori[sz]e|label this|which (?:intent|category))\b", re.I), "json_schema", "classification"),
    ("extract_json", re.compile(r"\b(extract|parse|pull out|scrape)\b.*\b(json|csv|schema|field|column|row)\b", re.I), "json_schema", "extraction_records"),
    ("extract_json", re.compile(r"\b(json schema|structured (?:data|output))\b", re.I), "json_schema", "json_object"),
    ("regex_gen",    re.compile(r"\b(regex|regular expression)\b.*\b(build|write|generate|make)\b", re.I), "ast", None),
    ("touch_fix",    re.compile(r"\b(fix|debug|repair)\b.*\b(code|error|exception|traceback)\b", re.I), "ast", None),
    ("math",         re.compile(r"\b(calculate|compute|solve)\b|^\s*\d+\s*[\+\-\*\/]\s*\d+", re.I), "numeric", None),
    ("summarize",    re.compile(r"\b(summari[sz]e|tl;?dr|shorten)\b", re.I), "rouge", None),
)

TASK_CLASSES: tuple[str, ...] = tuple(r[0] for r in _CLASS_RULES) + ("unknown",)
_LOCAL_ELIGIBLE_KINDS = frozenset({"json_schema", "ast", "numeric"})
_ROUGE_ELIGIBLE_TIERS = frozenset({"pro", "business"})


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: str
    task_class: str
    verifier_kind: str
    grammar_id: str | None
    confidence: float
    reason: str
    signals: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route, "task_class": self.task_class,
            "verifier_kind": self.verifier_kind,
            "grammar_id": self.grammar_id,
            "confidence": round(self.confidence, 3),
            "reason": self.reason, "signals": list(self.signals),
        }


_API_ONLY = RouteDecision("api", "unknown", "none", None, 0.0, "default_api")


def classify(
    prompt: str,
    files: list[dict] | None = None,
    *,
    intent: str = "",
    tier: str = "free",
    workspace: str = "core",
) -> RouteDecision:
    """
    Deterministic L1 decision. Never raises.
    AXELR_LOCAL_ROUTER: off → api; shadow → api but logs; on → real.
    """
    if _MODE == "off" or not prompt or len(prompt) < 6:
        return _API_ONLY

    head = prompt[:1200]
    match = None
    for cls, rx, kind, grammar in _CLASS_RULES:
        try:
            if rx.search(head):
                match = (cls, kind, grammar)
                break
        except Exception:
            continue
    if match is None:
        return _API_ONLY

    task_class, verifier_kind, grammar_id = match

    if verifier_kind in _LOCAL_ELIGIBLE_KINDS:
        route, reason = "local", f"verifiable:{verifier_kind}"
    elif verifier_kind == "rouge" and tier in _ROUGE_ELIGIBLE_TIERS:
        route, reason = "hybrid", "rouge_paid_tier"
    else:
        return RouteDecision("api", task_class, verifier_kind, grammar_id,
                             0.3, "not_locally_verifiable")

    confidence = 0.85 if grammar_id else 0.70
    signals = (f"class:{task_class}", f"intent:{intent or 'auto'}",
               f"workspace:{workspace}")

    if _MODE == "shadow":
        logger.info("router_shadow would_route=%s class=%s kind=%s",
                    route, task_class, verifier_kind)
        return RouteDecision("api", task_class, verifier_kind, grammar_id,
                             confidence, f"shadow:{reason}", signals)

    return RouteDecision(route, task_class, verifier_kind, grammar_id,
                         confidence, reason, signals)


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Grammar Engine
# ═══════════════════════════════════════════════════════════════════════════
GRAMMAR_DIR = Path(os.getenv("AXELR_GRAMMAR_DIR", "grammars")).resolve()
GRAMMAR_CACHE_DIR = GRAMMAR_DIR / "cache"
_SAFE_ID_RE = re.compile(r"[^a-z0-9_]+")
_MAX_DEPTH = 8

_GBNF_HEADER = "# autogenerated by axelr.core.structured_gen — do not edit\n"
_JSON_PRIMITIVES = """\
json_value ::= json_object | json_array | json_string | json_number | "true" | "false" | "null"
json_object ::= "{" ws (json_string ws ":" ws json_value ("," ws json_string ws ":" ws json_value)*)? ws "}"
json_array ::= "[" ws (json_value ("," ws json_value)*)? ws "]"
json_string ::= "\\"" ([^"\\\\] | "\\\\" (["\\\\/bfnrt] | "u" [0-9a-fA-F]{4}))* "\\""
json_number ::= "-"? ([0-9] | [1-9][0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)?
ws ::= [ \\t\\n\\r]*
"""


def _safe_id(name: str) -> str:
    return _SAFE_ID_RE.sub("_", name.lower()).strip("_") or "unnamed"


def _gbnf_string_literal(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _schema_to_gbnf(schema: dict[str, Any], rule_name: str,
                    depth: int = 0) -> list[str]:
    if depth > _MAX_DEPTH:
        return [f"{rule_name} ::= json_value"]
    t = schema.get("type")
    rules: list[str] = []

    if t == "object":
        props = schema.get("properties") or {}
        if not props:
            return [f"{rule_name} ::= json_object"]
        pair_names: list[str] = []
        for key, sub in props.items():
            sub_rule = f"{rule_name}_{_safe_id(key)}"
            rules.extend(_schema_to_gbnf(sub, sub_rule, depth + 1))
            pname = f"{rule_name}_pair_{_safe_id(key)}"
            pair_names.append(pname)
            rules.append(
                f'{pname} ::= {_gbnf_string_literal(key)} ws ":" ws {sub_rule}'
            )
        pair_alt = " | ".join(pair_names)
        rules.append(f'{rule_name}_inner ::= {pair_alt} ("," ws ({pair_alt}))*')
        rules.append(f'{rule_name} ::= "{{" ws {rule_name}_inner? ws "}}"')
        return rules

    if t == "array":
        items = schema.get("items") or {}
        item_rule = f"{rule_name}_item"
        rules.extend(_schema_to_gbnf(items, item_rule, depth + 1))
        rules.append(f'{rule_name} ::= "[" ws ({item_rule} ("," ws {item_rule})*)? ws "]"')
        return rules

    if t == "string":
        enum = schema.get("enum")
        if enum:
            alts = " | ".join(_gbnf_string_literal(str(v)) for v in enum)
            rules.append(f"{rule_name} ::= {alts}")
        else:
            rules.append(f"{rule_name} ::= json_string")
        return rules

    if t == "integer":
        return [f'{rule_name} ::= "-"? [0-9]+']
    if t == "number":
        return [f'{rule_name} ::= "-"? [0-9]+ ("." [0-9]+)? ([eE] [-+]? [0-9]+)?']
    if t == "boolean":
        return [f'{rule_name} ::= "true" | "false"']
    if t == "null":
        return [f'{rule_name} ::= "null"']
    return [f"{rule_name} ::= json_value"]


def compile_schema(schema: dict[str, Any]) -> str | None:
    """Compile JSON Schema → GBNF file. Returns absolute path or None."""
    if not isinstance(schema, dict) or not schema:
        return None
    try:
        rules = _schema_to_gbnf(schema, "root")
        text = _GBNF_HEADER + _JSON_PRIMITIVES + "\n" + "\n".join(rules) + "\n"
    except Exception as e:
        logger.warning("grammar_compile_failed error=%s", e)
        return None
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        GRAMMAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = GRAMMAR_CACHE_DIR / f"schema_{digest}.gbnf"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        return str(path)
    except Exception as e:
        logger.warning("grammar_cache_write_failed error=%s", e)
        return None


def get_grammar(grammar_id: str) -> str | None:
    """Return path to a static grammar file, or None."""
    if not grammar_id:
        return None
    path = GRAMMAR_DIR / f"{_safe_id(grammar_id)}.gbnf"
    return str(path) if path.is_file() else None


# ═══════════════════════════════════════════════════════════════════════════
# Static grammar resolver — used by core.twin_engine to constrain local
# model output for well-known task types. Files live in GRAMMAR_DIR.
# ═══════════════════════════════════════════════════════════════════════════
_STATIC_GRAMMARS: dict[str, str] = {
    # task_type -> static .gbnf filename (joined to GRAMMAR_DIR verbatim;
    # not passed through _safe_id, which would strip the extension).
    "extraction":     "extraction_records.gbnf",
    "structuring":    "json_object.gbnf",
    "tool_call":      "tool_call.gbnf",
    "classification": "classification.gbnf",
    "pii_scan":       "pii_spans.gbnf",
    # These task types are documented for completeness — no grammar
    # applies, so free-form sampling is used.
    "frontend":       "",
    "touch_fix":      "",
}


def grammar_file_for(task_type: str, workspace: str = "") -> str | None:
    """
    Return an absolute path to a GBNF grammar that constrains the local
    model's output for `task_type`, or None if no grammar applies.

    Returned paths are validated (must exist). Missing grammars return
    None so the caller silently falls through to unconstrained sampling.
    """
    name = _STATIC_GRAMMARS.get((task_type or "").lower())
    if not name:
        return None
    path = GRAMMAR_DIR / name
    if not path.is_file():
        return None
    return str(path)


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Local Verifier
# ═══════════════════════════════════════════════════════════════════════════
_V_MODE = os.getenv("AXELR_LOCAL_VERIFY", "off").strip().lower()
if _V_MODE not in ("off", "shadow", "on"):
    _V_MODE = "off"

VerifierKind = str  # "json_schema" | "ast" | "numeric" | "rouge" | "none"
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_FENCE_RE = re.compile(r"```(?:python|py|python3)?\s*\n([\s\S]*?)```", re.I)


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    reason: str
    kind: str
    degraded: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reason": self.reason, "kind": self.kind,
                "degraded": self.degraded, "details": self.details}


def _inline_schema_check(text: str, schema: dict[str, Any]) -> tuple[bool, str, dict]:
    try:
        data = json.loads(text)
    except Exception as e:
        return False, "json_parse", {"error": str(e)[:120]}

    def _check(node: Any, s: dict[str, Any], path: str) -> str | None:
        t = s.get("type")
        if t == "object":
            if not isinstance(node, dict):
                return f"{path}:not_object"
            for k in (s.get("required") or []):
                if k not in node:
                    return f"{path}:missing:{k}"
            for k, sub in (s.get("properties") or {}).items():
                if k in node:
                    err = _check(node[k], sub, f"{path}.{k}")
                    if err:
                        return err
            return None
        if t == "array":
            if not isinstance(node, list):
                return f"{path}:not_array"
            items = s.get("items")
            if items:
                for i, v in enumerate(node[:50]):
                    err = _check(v, items, f"{path}[{i}]")
                    if err:
                        return err
            return None
        if t == "string":
            if not isinstance(node, str):
                return f"{path}:not_string"
            enum = s.get("enum")
            if enum and node not in enum:
                return f"{path}:not_in_enum"
            return None
        if t == "integer":
            if not isinstance(node, int) or isinstance(node, bool):
                return f"{path}:not_integer"
            return None
        if t == "number":
            if not isinstance(node, (int, float)) or isinstance(node, bool):
                return f"{path}:not_number"
            return None
        if t == "boolean":
            if not isinstance(node, bool):
                return f"{path}:not_bool"
            return None
        if t == "null":
            if node is not None:
                return f"{path}:not_null"
            return None
        return None

    err = _check(data, schema, "$")
    return (True, "ok", {}) if err is None else (False, "schema_violation", {"at": err})


def _try_jsonschema(text: str, schema: dict[str, Any]) -> tuple[bool, str, dict]:
    try:
        import jsonschema  # type: ignore
    except Exception:
        return _inline_schema_check(text, schema)
    try:
        data = json.loads(text)
    except Exception as e:
        return False, "json_parse", {"error": str(e)[:120]}
    try:
        jsonschema.validate(data, schema)
        return True, "ok", {}
    except jsonschema.ValidationError as e:  # type: ignore[attr-defined]
        return False, "schema_violation", {
            "path": list(e.absolute_path)[:4], "msg": e.message[:120]}
    except Exception as e:
        return False, "schema_error", {"error": str(e)[:120]}


def _verify_ast(text: str) -> tuple[bool, str, dict]:
    m = _FENCE_RE.search(text)
    code = m.group(1) if m else text
    try:
        ast.parse(code)
        return True, "ok", {"bytes": len(code)}
    except SyntaxError as e:
        return False, "syntax_error", {"line": e.lineno, "msg": (e.msg or "")[:80]}
    except Exception as e:
        return False, "ast_error", {"error": str(e)[:80]}


_ARITH_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*([\+\-\*/])\s*(-?\d+(?:\.\d+)?)")
_SAFE_OP = {
    "+": lambda a, b: a + b, "-": lambda a, b: a - b,
    "*": lambda a, b: a * b, "/": lambda a, b: a / b if b != 0 else None,
}


def _verify_numeric(text: str, prompt: str) -> tuple[bool, str, dict]:
    exprs = _ARITH_RE.findall(prompt or "")
    if not exprs:
        return True, "ok_no_numeric_claim", {}
    required: list[str] = []
    for a_s, op, b_s in exprs[:8]:
        try:
            r = _SAFE_OP[op](float(a_s), float(b_s))
            if r is None:
                continue
            if r == int(r):
                required.append(str(int(r)))
            required.append(f"{r:.4f}".rstrip("0").rstrip("."))
        except Exception:
            continue
    if not required:
        return True, "ok_no_required_numbers", {}
    missing = [n for n in required if n not in (text or "")]
    if missing:
        return False, "numeric_mismatch", {"missing": missing[:4],
                                            "required": required[:4]}
    return True, "ok", {"checked": len(required)}


def _verify_rouge(text: str, source: str) -> tuple[bool, str, dict]:
    if not source:
        return True, "no_source", {}
    a = [w.lower() for w in (text or "").split()]
    b = [w.lower() for w in source.split()]
    if not a or not b:
        return False, "empty", {}
    overlap = sum(1 for w in a if w in set(b))
    ratio = overlap / max(1, len(a))
    if ratio < 0.35:
        return False, "low_overlap", {"overlap_ratio": round(ratio, 3)}
    if len(a) > 0.6 * len(b):
        return False, "summary_too_long", {"ratio_len": round(len(a) / len(b), 2)}
    return True, "ok", {"overlap_ratio": round(ratio, 3)}


def verify(
    text: str,
    *,
    kind: VerifierKind,
    schema: dict[str, Any] | None = None,
    source: str = "",
    prompt: str = "",
) -> Verdict:
    """Dispatch by kind. Never raises. Fails open on unknown kinds."""
    if _V_MODE == "off":
        return Verdict(True, "verify_disabled", kind, degraded=True)
    if not text:
        return Verdict(False, "empty_output", kind)

    try:
        if kind == "json_schema":
            if not schema:
                return Verdict(True, "no_schema", kind, degraded=True)
            passed, reason, details = _try_jsonschema(text, schema)
        elif kind == "ast":
            passed, reason, details = _verify_ast(text)
        elif kind == "numeric":
            passed, reason, details = _verify_numeric(text, prompt)
        elif kind == "rouge":
            passed, reason, details = _verify_rouge(text, source)
        elif kind in ("none", None):
            return Verdict(True, "no_verifier", kind, degraded=True)
        else:
            return Verdict(True, f"unknown_kind:{kind}", kind, degraded=True)
    except Exception as e:
        logger.warning("verify_internal_error kind=%s error=%s", kind, e)
        return Verdict(True, "verifier_exception", kind, degraded=True,
                       details={"error": str(e)[:120]})

    if _V_MODE == "shadow" and not passed:
        logger.info("verifier_shadow would_fail kind=%s reason=%s", kind, reason)
        return Verdict(True, f"shadow:{reason}", kind, details=details)

=======
# core/structured_gen.py
"""
AXELR — Structured Generation Layer (routing · grammar · verification)
========================================================================
Three primitives that turn a general-purpose LLM call into a *verifiable*
structured-output call. Previously split across `task_router_v2.py`,
`grammar_engine.py`, and `local_verifier.py`; merged because a change to
what "json_schema-verifiable" *means* must touch exactly one file.

    §1  Task Router      classify(prompt, ...) -> RouteDecision
    §2  Grammar Engine   compile_schema(schema) -> GBNF path
    §3  Local Verifier   verify(text, kind=...) -> Verdict

Every function is deterministic, bounded, and never raises. Consumed by
`core.elite_orchestrator.hedge_wrap` and `api._boot._install_local_router`.

Env gates
---------
  AXELR_LOCAL_ROUTER  = off | shadow | on   (default off)
  AXELR_LOCAL_VERIFY  = off | shadow | on   (default off)
  AXELR_GRAMMAR_DIR   = ./grammars
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("axelr.structured_gen")

__all__ = [
    # §1 Router
    "classify", "RouteDecision", "TASK_CLASSES",
    # §2 Grammar
    "compile_schema", "get_grammar", "GRAMMAR_DIR", "GRAMMAR_CACHE_DIR",
    # §3 Verifier
    "verify", "Verdict", "VerifierKind",
]


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Task Router
# ═══════════════════════════════════════════════════════════════════════════
_MODE = os.getenv("AXELR_LOCAL_ROUTER", "off").strip().lower()
if _MODE not in ("off", "shadow", "on"):
    _MODE = "off"

# (task_class, pattern, verifier_kind, grammar_id). First match wins.
_CLASS_RULES: tuple[tuple[str, re.Pattern[str], str, str | None], ...] = (
    ("pii_scan",     re.compile(r"\b(pii|personally identifiable|redact|anonymi[sz]e)\b", re.I), "json_schema", "pii_spans"),
    ("tool_call",    re.compile(r"\b(function[- ]?call|tool[- ]?call|call the \w+ tool)\b", re.I), "json_schema", "tool_call"),
    ("classify",     re.compile(r"\b(classify|categori[sz]e|label this|which (?:intent|category))\b", re.I), "json_schema", "classification"),
    ("extract_json", re.compile(r"\b(extract|parse|pull out|scrape)\b.*\b(json|csv|schema|field|column|row)\b", re.I), "json_schema", "extraction_records"),
    ("extract_json", re.compile(r"\b(json schema|structured (?:data|output))\b", re.I), "json_schema", "json_object"),
    ("regex_gen",    re.compile(r"\b(regex|regular expression)\b.*\b(build|write|generate|make)\b", re.I), "ast", None),
    ("touch_fix",    re.compile(r"\b(fix|debug|repair)\b.*\b(code|error|exception|traceback)\b", re.I), "ast", None),
    ("math",         re.compile(r"\b(calculate|compute|solve)\b|^\s*\d+\s*[\+\-\*\/]\s*\d+", re.I), "numeric", None),
    ("summarize",    re.compile(r"\b(summari[sz]e|tl;?dr|shorten)\b", re.I), "rouge", None),
)

TASK_CLASSES: tuple[str, ...] = tuple(r[0] for r in _CLASS_RULES) + ("unknown",)
_LOCAL_ELIGIBLE_KINDS = frozenset({"json_schema", "ast", "numeric"})
_ROUGE_ELIGIBLE_TIERS = frozenset({"pro", "business"})


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: str
    task_class: str
    verifier_kind: str
    grammar_id: str | None
    confidence: float
    reason: str
    signals: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route, "task_class": self.task_class,
            "verifier_kind": self.verifier_kind,
            "grammar_id": self.grammar_id,
            "confidence": round(self.confidence, 3),
            "reason": self.reason, "signals": list(self.signals),
        }


_API_ONLY = RouteDecision("api", "unknown", "none", None, 0.0, "default_api")


def classify(
    prompt: str,
    files: list[dict] | None = None,
    *,
    intent: str = "",
    tier: str = "free",
    workspace: str = "core",
) -> RouteDecision:
    """
    Deterministic L1 decision. Never raises.
    AXELR_LOCAL_ROUTER: off → api; shadow → api but logs; on → real.
    """
    if _MODE == "off" or not prompt or len(prompt) < 6:
        return _API_ONLY

    head = prompt[:1200]
    match = None
    for cls, rx, kind, grammar in _CLASS_RULES:
        try:
            if rx.search(head):
                match = (cls, kind, grammar)
                break
        except Exception:
            continue
    if match is None:
        return _API_ONLY

    task_class, verifier_kind, grammar_id = match

    if verifier_kind in _LOCAL_ELIGIBLE_KINDS:
        route, reason = "local", f"verifiable:{verifier_kind}"
    elif verifier_kind == "rouge" and tier in _ROUGE_ELIGIBLE_TIERS:
        route, reason = "hybrid", "rouge_paid_tier"
    else:
        return RouteDecision("api", task_class, verifier_kind, grammar_id,
                             0.3, "not_locally_verifiable")

    confidence = 0.85 if grammar_id else 0.70
    signals = (f"class:{task_class}", f"intent:{intent or 'auto'}",
               f"workspace:{workspace}")

    if _MODE == "shadow":
        logger.info("router_shadow would_route=%s class=%s kind=%s",
                    route, task_class, verifier_kind)
        return RouteDecision("api", task_class, verifier_kind, grammar_id,
                             confidence, f"shadow:{reason}", signals)

    return RouteDecision(route, task_class, verifier_kind, grammar_id,
                         confidence, reason, signals)


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Grammar Engine
# ═══════════════════════════════════════════════════════════════════════════
GRAMMAR_DIR = Path(os.getenv("AXELR_GRAMMAR_DIR", "grammars")).resolve()
GRAMMAR_CACHE_DIR = GRAMMAR_DIR / "cache"
_SAFE_ID_RE = re.compile(r"[^a-z0-9_]+")
_MAX_DEPTH = 8

_GBNF_HEADER = "# autogenerated by axelr.core.structured_gen — do not edit\n"
_JSON_PRIMITIVES = """\
json_value ::= json_object | json_array | json_string | json_number | "true" | "false" | "null"
json_object ::= "{" ws (json_string ws ":" ws json_value ("," ws json_string ws ":" ws json_value)*)? ws "}"
json_array ::= "[" ws (json_value ("," ws json_value)*)? ws "]"
json_string ::= "\\"" ([^"\\\\] | "\\\\" (["\\\\/bfnrt] | "u" [0-9a-fA-F]{4}))* "\\""
json_number ::= "-"? ([0-9] | [1-9][0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)?
ws ::= [ \\t\\n\\r]*
"""


def _safe_id(name: str) -> str:
    return _SAFE_ID_RE.sub("_", name.lower()).strip("_") or "unnamed"


def _gbnf_string_literal(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _schema_to_gbnf(schema: dict[str, Any], rule_name: str,
                    depth: int = 0) -> list[str]:
    if depth > _MAX_DEPTH:
        return [f"{rule_name} ::= json_value"]
    t = schema.get("type")
    rules: list[str] = []

    if t == "object":
        props = schema.get("properties") or {}
        if not props:
            return [f"{rule_name} ::= json_object"]
        pair_names: list[str] = []
        for key, sub in props.items():
            sub_rule = f"{rule_name}_{_safe_id(key)}"
            rules.extend(_schema_to_gbnf(sub, sub_rule, depth + 1))
            pname = f"{rule_name}_pair_{_safe_id(key)}"
            pair_names.append(pname)
            rules.append(
                f'{pname} ::= {_gbnf_string_literal(key)} ws ":" ws {sub_rule}'
            )
        pair_alt = " | ".join(pair_names)
        rules.append(f'{rule_name}_inner ::= {pair_alt} ("," ws ({pair_alt}))*')
        rules.append(f'{rule_name} ::= "{{" ws {rule_name}_inner? ws "}}"')
        return rules

    if t == "array":
        items = schema.get("items") or {}
        item_rule = f"{rule_name}_item"
        rules.extend(_schema_to_gbnf(items, item_rule, depth + 1))
        rules.append(f'{rule_name} ::= "[" ws ({item_rule} ("," ws {item_rule})*)? ws "]"')
        return rules

    if t == "string":
        enum = schema.get("enum")
        if enum:
            alts = " | ".join(_gbnf_string_literal(str(v)) for v in enum)
            rules.append(f"{rule_name} ::= {alts}")
        else:
            rules.append(f"{rule_name} ::= json_string")
        return rules

    if t == "integer":
        return [f'{rule_name} ::= "-"? [0-9]+']
    if t == "number":
        return [f'{rule_name} ::= "-"? [0-9]+ ("." [0-9]+)? ([eE] [-+]? [0-9]+)?']
    if t == "boolean":
        return [f'{rule_name} ::= "true" | "false"']
    if t == "null":
        return [f'{rule_name} ::= "null"']
    return [f"{rule_name} ::= json_value"]


def compile_schema(schema: dict[str, Any]) -> str | None:
    """Compile JSON Schema → GBNF file. Returns absolute path or None."""
    if not isinstance(schema, dict) or not schema:
        return None
    try:
        rules = _schema_to_gbnf(schema, "root")
        text = _GBNF_HEADER + _JSON_PRIMITIVES + "\n" + "\n".join(rules) + "\n"
    except Exception as e:
        logger.warning("grammar_compile_failed error=%s", e)
        return None
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        GRAMMAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = GRAMMAR_CACHE_DIR / f"schema_{digest}.gbnf"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        return str(path)
    except Exception as e:
        logger.warning("grammar_cache_write_failed error=%s", e)
        return None


def get_grammar(grammar_id: str) -> str | None:
    """Return path to a static grammar file, or None."""
    if not grammar_id:
        return None
    path = GRAMMAR_DIR / f"{_safe_id(grammar_id)}.gbnf"
    return str(path) if path.is_file() else None


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Local Verifier
# ═══════════════════════════════════════════════════════════════════════════
_V_MODE = os.getenv("AXELR_LOCAL_VERIFY", "off").strip().lower()
if _V_MODE not in ("off", "shadow", "on"):
    _V_MODE = "off"

VerifierKind = str  # "json_schema" | "ast" | "numeric" | "rouge" | "none"
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_FENCE_RE = re.compile(r"```(?:python|py|python3)?\s*\n([\s\S]*?)```", re.I)


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    reason: str
    kind: str
    degraded: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reason": self.reason, "kind": self.kind,
                "degraded": self.degraded, "details": self.details}


def _inline_schema_check(text: str, schema: dict[str, Any]) -> tuple[bool, str, dict]:
    try:
        data = json.loads(text)
    except Exception as e:
        return False, "json_parse", {"error": str(e)[:120]}

    def _check(node: Any, s: dict[str, Any], path: str) -> str | None:
        t = s.get("type")
        if t == "object":
            if not isinstance(node, dict):
                return f"{path}:not_object"
            for k in (s.get("required") or []):
                if k not in node:
                    return f"{path}:missing:{k}"
            for k, sub in (s.get("properties") or {}).items():
                if k in node:
                    err = _check(node[k], sub, f"{path}.{k}")
                    if err:
                        return err
            return None
        if t == "array":
            if not isinstance(node, list):
                return f"{path}:not_array"
            items = s.get("items")
            if items:
                for i, v in enumerate(node[:50]):
                    err = _check(v, items, f"{path}[{i}]")
                    if err:
                        return err
            return None
        if t == "string":
            if not isinstance(node, str):
                return f"{path}:not_string"
            enum = s.get("enum")
            if enum and node not in enum:
                return f"{path}:not_in_enum"
            return None
        if t == "integer":
            if not isinstance(node, int) or isinstance(node, bool):
                return f"{path}:not_integer"
            return None
        if t == "number":
            if not isinstance(node, (int, float)) or isinstance(node, bool):
                return f"{path}:not_number"
            return None
        if t == "boolean":
            if not isinstance(node, bool):
                return f"{path}:not_bool"
            return None
        if t == "null":
            if node is not None:
                return f"{path}:not_null"
            return None
        return None

    err = _check(data, schema, "$")
    return (True, "ok", {}) if err is None else (False, "schema_violation", {"at": err})


def _try_jsonschema(text: str, schema: dict[str, Any]) -> tuple[bool, str, dict]:
    try:
        import jsonschema  # type: ignore
    except Exception:
        return _inline_schema_check(text, schema)
    try:
        data = json.loads(text)
    except Exception as e:
        return False, "json_parse", {"error": str(e)[:120]}
    try:
        jsonschema.validate(data, schema)
        return True, "ok", {}
    except jsonschema.ValidationError as e:  # type: ignore[attr-defined]
        return False, "schema_violation", {
            "path": list(e.absolute_path)[:4], "msg": e.message[:120]}
    except Exception as e:
        return False, "schema_error", {"error": str(e)[:120]}


def _verify_ast(text: str) -> tuple[bool, str, dict]:
    m = _FENCE_RE.search(text)
    code = m.group(1) if m else text
    try:
        ast.parse(code)
        return True, "ok", {"bytes": len(code)}
    except SyntaxError as e:
        return False, "syntax_error", {"line": e.lineno, "msg": (e.msg or "")[:80]}
    except Exception as e:
        return False, "ast_error", {"error": str(e)[:80]}


_ARITH_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*([\+\-\*/])\s*(-?\d+(?:\.\d+)?)")
_SAFE_OP = {
    "+": lambda a, b: a + b, "-": lambda a, b: a - b,
    "*": lambda a, b: a * b, "/": lambda a, b: a / b if b != 0 else None,
}


def _verify_numeric(text: str, prompt: str) -> tuple[bool, str, dict]:
    exprs = _ARITH_RE.findall(prompt or "")
    if not exprs:
        return True, "ok_no_numeric_claim", {}
    required: list[str] = []
    for a_s, op, b_s in exprs[:8]:
        try:
            r = _SAFE_OP[op](float(a_s), float(b_s))
            if r is None:
                continue
            if r == int(r):
                required.append(str(int(r)))
            required.append(f"{r:.4f}".rstrip("0").rstrip("."))
        except Exception:
            continue
    if not required:
        return True, "ok_no_required_numbers", {}
    missing = [n for n in required if n not in (text or "")]
    if missing:
        return False, "numeric_mismatch", {"missing": missing[:4],
                                            "required": required[:4]}
    return True, "ok", {"checked": len(required)}


def _verify_rouge(text: str, source: str) -> tuple[bool, str, dict]:
    if not source:
        return True, "no_source", {}
    a = [w.lower() for w in (text or "").split()]
    b = [w.lower() for w in source.split()]
    if not a or not b:
        return False, "empty", {}
    overlap = sum(1 for w in a if w in set(b))
    ratio = overlap / max(1, len(a))
    if ratio < 0.35:
        return False, "low_overlap", {"overlap_ratio": round(ratio, 3)}
    if len(a) > 0.6 * len(b):
        return False, "summary_too_long", {"ratio_len": round(len(a) / len(b), 2)}
    return True, "ok", {"overlap_ratio": round(ratio, 3)}


def verify(
    text: str,
    *,
    kind: VerifierKind,
    schema: dict[str, Any] | None = None,
    source: str = "",
    prompt: str = "",
) -> Verdict:
    """Dispatch by kind. Never raises. Fails open on unknown kinds."""
    if _V_MODE == "off":
        return Verdict(True, "verify_disabled", kind, degraded=True)
    if not text:
        return Verdict(False, "empty_output", kind)

    try:
        if kind == "json_schema":
            if not schema:
                return Verdict(True, "no_schema", kind, degraded=True)
            passed, reason, details = _try_jsonschema(text, schema)
        elif kind == "ast":
            passed, reason, details = _verify_ast(text)
        elif kind == "numeric":
            passed, reason, details = _verify_numeric(text, prompt)
        elif kind == "rouge":
            passed, reason, details = _verify_rouge(text, source)
        elif kind in ("none", None):
            return Verdict(True, "no_verifier", kind, degraded=True)
        else:
            return Verdict(True, f"unknown_kind:{kind}", kind, degraded=True)
    except Exception as e:
        logger.warning("verify_internal_error kind=%s error=%s", kind, e)
        return Verdict(True, "verifier_exception", kind, degraded=True,
                       details={"error": str(e)[:120]})

    if _V_MODE == "shadow" and not passed:
        logger.info("verifier_shadow would_fail kind=%s reason=%s", kind, reason)
        return Verdict(True, f"shadow:{reason}", kind, details=details)

>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
    return Verdict(passed, reason, kind, details=details)