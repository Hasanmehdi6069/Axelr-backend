<<<<<<< HEAD
# core/hardening.py
"""
AXELR — Output Hardening (pruning · hyperparams · AST · task-split · telemetry)
================================================================================
Five orthogonal, zero-cost primitives that raise quality without touching
the provider layer. Previously `hyperparams.py`, `ast_repair.py`,
`task_split.py`, `provider_telemetry.py` — merged so every "output cleanup"
concern lives in one auditable file.

    §1  prune_system       strip filler/hedges from prose (keeps code)
    §2  derive/payload_extras  complexity → Top-P / penalties
    §3  repair             deterministic AST/JSON fenced-block repair
    §4  decompose/race_tasks  split multi-part prompts, parallel-race them
    §5  RING               short-window provider health ring

Every function is deterministic, pure-stdlib, and never raises.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("axelr.hardening")

__all__ = [
    # §1 pruning
    "prune_system",
    # §2 hyperparams
    "Hyperparams", "derive", "payload_extras",
    # §3 repair
    "ast_repair",
    # §4 task split
    "decompose", "race_tasks", "is_decomposable",
    # §5 telemetry
    "RING", "TelemetryRing", "ProviderEvent",
    # audit
    "audit",
]


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Prompt pruning
# ═══════════════════════════════════════════════════════════════════════════
_FENCE_RE = re.compile(r"```[\s\S]*?```")
_PROSE_FILLER_RE = re.compile(
    r"\b(?:please|kindly|just|really|very|actually|basically|simply|"
    r"maybe|perhaps|you know|like|literally|honestly|frankly|"
    r"if you don'?t mind|if possible|when you can|at your earliest)\b", re.I)
_HEDGE_RE = re.compile(
    r"\b(?:i think|i believe|it seems|it appears|possibly|probably|"
    r"might be|may be|could be|sort of|kind of|more or less)\b", re.I)
_INTENSIFIER_RE = re.compile(
    r"\b(?:very|extremely|incredibly|absolutely|totally|completely|"
    r"utterly|highly|deeply|strongly)\s+", re.I)
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_MULTINL_RE = re.compile(r"\n{3,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")


def prune_system(text: str) -> str:
    """Strip low-info tokens from prose. Preserves code fences verbatim.
    Idempotent: prune(prune(x)) == prune(x)."""
    if not text or not text.strip():
        return text or ""
    fences: list[str] = []

    def _stash(m: re.Match) -> str:
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    stashed = _FENCE_RE.sub(_stash, text)
    stashed = _HEDGE_RE.sub("", stashed)
    stashed = _PROSE_FILLER_RE.sub("", stashed)
    stashed = _INTENSIFIER_RE.sub("", stashed)
    stashed = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", stashed)
    stashed = _MULTISPACE_RE.sub(" ", stashed)
    stashed = _MULTINL_RE.sub("\n\n", stashed)

    def _restore(m: re.Match) -> str:
        idx = int(m.group(1))
        return fences[idx] if 0 <= idx < len(fences) else m.group(0)

    return re.sub(r"\x00FENCE(\d+)\x00", _restore, stashed).strip()


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Hyperparameters
# ═══════════════════════════════════════════════════════════════════════════
_CODE_HINT_RE = re.compile(
    r"```|\bdef\s|\bclass\s|\bfunction\s|\bconst\s|\bSELECT\s|\bINSERT\s")
_PUNCT_RE = re.compile(r"[.,;:!?]")


def _complexity(prompt: str, task_type: str = "", intent: str = "") -> float:
    if not prompt:
        return 0.0
    words = prompt.split()
    n = len(words)
    if n == 0:
        return 0.0
    length_sig = min(1.0, n / 200.0)
    vocab_div = min(1.0, len({w.lower() for w in words}) / n)
    punct_sig = min(1.0, len(_PUNCT_RE.findall(prompt)) / max(1.0, n / 8.0))
    code_sig = 1.0 if _CODE_HINT_RE.search(prompt) else 0.0
    c = 0.35 * length_sig + 0.25 * vocab_div + 0.15 * punct_sig + 0.25 * code_sig
    if task_type in ("extraction", "data") or intent == "extraction":
        c = min(c, 0.25)
    if task_type == "touch_fix":
        c = min(c, 0.45)
    return max(0.0, min(1.0, c))


@dataclass(frozen=True, slots=True)
class Hyperparams:
    top_p: float
    frequency_penalty: float
    presence_penalty: float
    complexity: float

    def to_dict(self) -> dict[str, float]:
        return {
            "top_p": round(self.top_p, 3),
            "frequency_penalty": round(self.frequency_penalty, 3),
            "presence_penalty": round(self.presence_penalty, 3),
            "complexity": round(self.complexity, 3),
        }


def derive(prompt: str, task_type: str = "", intent: str = "") -> Hyperparams:
    """Map complexity → (top_p, freq_pen, presence_pen). Deterministic."""
    c = _complexity(prompt, task_type, intent)
    if task_type in ("extraction", "data") or intent == "extraction":
        return Hyperparams(0.80, 0.0, 0.0, c)
    return Hyperparams(
        top_p=round(0.85 + 0.12 * c, 3),
        frequency_penalty=round(0.05 + 0.35 * c, 3),
        presence_penalty=round(0.00 + 0.20 * c, 3),
        complexity=round(c, 3),
    )


def payload_extras(prompt: str, task_type: str = "",
                   intent: str = "") -> dict[str, Any]:
    """Return only the OpenAI payload extras. Never raises."""
    try:
        hp = derive(prompt, task_type, intent)
        return {"top_p": hp.top_p, "frequency_penalty": hp.frequency_penalty,
                "presence_penalty": hp.presence_penalty}
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# §3 — AST repair (JSON + Python fenced blocks)
# ═══════════════════════════════════════════════════════════════════════════
_FENCE_LANG_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\n([\s\S]*?)```")
_JSON_LANGS = frozenset({"json", "jsonc", "json5"})
_PY_LANGS = frozenset({"python", "py", "python3"})
_BRACKETS = (("{", "}"), ("[", "]"), ("(", ")"))
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")


def _balance(text: str, open_ch: str, close_ch: str) -> tuple[str, int]:
    depth = 0
    in_str = False
    esc = False
    for ch in text:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth = max(0, depth - 1)
    return (text + close_ch * depth, depth) if depth > 0 else (text, 0)


def _repair_json(text: str) -> tuple[str, list[str]]:
    if not text.strip():
        return text, []
    try:
        json.loads(text)
        return text, []
    except Exception:
        pass
    issues: list[str] = []
    out = text
    stripped = _BLOCK_COMMENT_RE.sub("", out)
    stripped = _LINE_COMMENT_RE.sub("", stripped)
    if stripped != out:
        out = stripped; issues.append("stripped_js_comments")
    new = _TRAILING_COMMA_RE.sub(r"\1", out)
    if new != out:
        out = new; issues.append("stripped_trailing_comma")
    for o, c in _BRACKETS:
        out, fixed = _balance(out, o, c)
        if fixed:
            issues.append(f"closed_{o}{c}_x{fixed}")
    try:
        json.loads(out)
        return out, issues
    except Exception:
        return text, []


def _repair_python(text: str) -> tuple[str, list[str]]:
    if not text.strip():
        return text, []
    try:
        ast.parse(text)
        return text, []
    except SyntaxError:
        pass
    except Exception:
        return text, []
    issues: list[str] = []
    out = text
    for o, c in _BRACKETS:
        out, fixed = _balance(out, o, c)
        if fixed:
            issues.append(f"closed_{o}{c}_x{fixed}")
    if not out.endswith("\n"):
        out += "\n"; issues.append("appended_newline")
    try:
        ast.parse(out)
        return out, issues
    except Exception:
        return text, []


def ast_repair(text: str) -> tuple[str, list[str]]:
    """Walk every fenced block, dispatch by language, repair. Never raises."""
    if not text or "```" not in text:
        return text, []
    all_issues: list[str] = []

    def _handle(m: re.Match) -> str:
        lang = (m.group(1) or "").strip().lower()
        body = m.group(2)
        if lang in _JSON_LANGS:
            fixed, issues = _repair_json(body)
        elif lang in _PY_LANGS:
            fixed, issues = _repair_python(body)
        else:
            return m.group(0)
        if issues:
            all_issues.extend(f"{lang}:{i}" for i in issues)
            return f"```{m.group(1)}\n{fixed}```"
        return m.group(0)

    try:
        new_text = _FENCE_LANG_RE.sub(_handle, text)
    except Exception:
        return text, []
    return new_text, all_issues


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Task split (parallel race)
# ═══════════════════════════════════════════════════════════════════════════
RouteFn = Callable[..., Awaitable[dict[str, Any]]]
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$", re.MULTILINE)
_QUESTION_SPLIT_RE = re.compile(r"\?\s+(?=[A-Z])")
_MIN_CLAUSE = 8
_MAX_CLAUSE = 400


def _clean_clause(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().rstrip(";,")


def is_decomposable(prompt: str) -> bool:
    if not prompt or len(prompt) < 60:
        return False
    if len(_BULLET_RE.findall(prompt)) >= 2:
        return True
    parts = _QUESTION_SPLIT_RE.split(prompt)
    return len(parts) >= 2 and all(len(q.strip()) >= 30 for q in parts)


def decompose(prompt: str, max_tasks: int = 3) -> list[str]:
    if not prompt:
        return [prompt]
    max_tasks = max(1, int(max_tasks))
    bullets = _BULLET_RE.findall(prompt)
    cleaned = [_clean_clause(b) for b in bullets
               if _MIN_CLAUSE <= len(b) <= _MAX_CLAUSE]
    if len(cleaned) >= 2:
        return cleaned[:max_tasks]
    parts = _QUESTION_SPLIT_RE.split(prompt)
    parts = [_clean_clause(p) + ("?" if not p.rstrip().endswith("?") else "")
             for p in parts if p.strip()]
    if len(parts) >= 2 and all(len(p) >= 30 for p in parts):
        return parts[:max_tasks]
    return [prompt]


_MERGE_PROMPT = """\
<task_merge>
You received partial answers to sub-questions of one larger request.

Original request:
{prompt}

Sub-answers (from parallel workers):
{subs}

Produce ONE coherent, unified answer. Preserve every fact, identifier,
and code block. Remove duplication. No meta-commentary, no preamble.
</task_merge>"""


async def _one(route_fn: RouteFn, prompt: str, **kw) -> dict[str, Any]:
    try:
        r = await route_fn(**{**kw, "prompt": prompt})
    except Exception as e:
        logger.debug("task_split_subcall_failed error=%s", e)
        return {"success": False, "text": "", "provider": "none"}
    return r if isinstance(r, dict) else {"success": False, "text": "",
                                          "provider": "none"}


async def race_tasks(
    route_fn: RouteFn, *,
    workspace: str, task_type: str, prompt: str,
    history: Any, files: Any,
    max_tokens: int, temp: float, tier: str,
    user: dict | None, context: str, request: Any,
    max_tasks: int = 3, timeout_s: float = 20.0,
) -> dict[str, Any] | None:
    """Fire N concurrent sub-calls + one merge call. Returns None on non-decomposable."""
    if not is_decomposable(prompt):
        return None
    subs = decompose(prompt, max_tasks=max_tasks)
    if len(subs) <= 1:
        return None
    sub_tokens = max(256, max_tokens // max(2, len(subs) + 1))
    tasks = [
        asyncio.create_task(_one(
            route_fn, sub,
            workspace=workspace, task_type=task_type,
            history=None, files=files,
            max_tokens=sub_tokens, temp=temp, tier=tier,
            user=user, context=context, request=request,
        ), name=f"split::{i}")
        for i, sub in enumerate(subs)
    ]
    done, pending = await asyncio.wait(tasks, timeout=max(4.0, float(timeout_s)))
    for t in pending:
        if not t.done():
            t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    collected: list[str] = []
    for sub, t in zip(subs, tasks):
        try:
            r = t.result() if t.done() and not t.cancelled() else None
        except Exception:
            r = None
        if isinstance(r, dict) and r.get("success") and r.get("text"):
            collected.append(f"Q: {sub}\nA: {r['text'].strip()}")
    if len(collected) < 2:
        return None
    subs_block = "\n\n".join(collected)
    merge_prompt = _MERGE_PROMPT.format(
        prompt=prompt[:1500], subs=subs_block[:6000])
    try:
        merged = await asyncio.wait_for(
            route_fn(workspace, task_type, merge_prompt,
                     None, None, max_tokens, temp, tier,
                     user, context, request),
            timeout=max(6.0, float(timeout_s)),
        )
    except Exception as e:
        logger.debug("task_split_merge_failed error=%s", e)
        return None
    if not isinstance(merged, dict) or not merged.get("success"):
        return None
    merged["task_split"] = {"sub_tasks": len(subs), "collected": len(collected)}
    return merged


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Provider telemetry ring (20-slot)
# ═══════════════════════════════════════════════════════════════════════════
_SLOT_SIZE = 20


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    provider: str
    success: bool
    latency_ms: float
    ts: float


class TelemetryRing:
    __slots__ = ("_lock", "_rings", "_size")

    def __init__(self, slot_size: int = _SLOT_SIZE) -> None:
        self._lock = threading.Lock()
        self._rings: dict[tuple[str, str], deque[ProviderEvent]] = {}
        self._size = max(4, int(slot_size))

    def _bucket(self, workspace: str, tier: str) -> deque[ProviderEvent]:
        key = (workspace or "core", tier or "free")
        d = self._rings.get(key)
        if d is None:
            d = deque(maxlen=self._size)
            self._rings[key] = d
        return d

    def record(self, workspace: str, tier: str, provider: str,
               success: bool, latency_ms: float) -> None:
        if not provider:
            return
        try:
            ev = ProviderEvent(provider=provider, success=bool(success),
                               latency_ms=float(latency_ms), ts=time.time())
        except Exception:
            return
        with self._lock:
            self._bucket(workspace, tier).append(ev)

    def _rate(self, d: deque[ProviderEvent], provider: str) -> tuple[float, float]:
        hits = [e for e in d if e.provider == provider]
        if not hits:
            return 0.5, 0.0
        ok = sum(1 for e in hits if e.success)
        return ok / len(hits), sum(e.latency_ms for e in hits) / len(hits)

    def recommend(self, workspace: str, tier: str,
                  candidates: list[str]) -> list[str]:
        if not candidates:
            return candidates
        with self._lock:
            d = self._rings.get((workspace or "core", tier or "free"))
            if not d:
                return list(candidates)
            scored = [((-self._rate(d, p)[0], self._rate(d, p)[1], i), p)
                      for i, p in enumerate(candidates)]
        scored.sort()
        return [p for _, p in scored]

    def snapshot(self, workspace: str, tier: str) -> dict[str, Any]:
        with self._lock:
            d = self._rings.get((workspace or "core", tier or "free"))
            if not d:
                return {"size": 0, "providers": {}}
            providers: dict[str, dict[str, float]] = {}
            for e in d:
                r = providers.setdefault(e.provider,
                                         {"hits": 0, "ok": 0, "lat": 0.0})
                r["hits"] += 1
                r["ok"] += 1 if e.success else 0
                r["lat"] += e.latency_ms
            for r in providers.values():
                if r["hits"]:
                    r["rate"] = round(r["ok"] / r["hits"], 3)
                    r["avg_ms"] = round(r["lat"] / r["hits"], 2)
                r.pop("lat", None)
        return {"size": len(d), "providers": providers}


RING = TelemetryRing()


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "pruning": True,
        "hyperparams": True,
        "ast_repair": True,
        "task_split": True,
        "telemetry_slots": _SLOT_SIZE,
        "telemetry_buckets": len(RING._rings),
=======
# core/hardening.py
"""
AXELR — Output Hardening (pruning · hyperparams · AST · task-split · telemetry)
================================================================================
Five orthogonal, zero-cost primitives that raise quality without touching
the provider layer. Previously `hyperparams.py`, `ast_repair.py`,
`task_split.py`, `provider_telemetry.py` — merged so every "output cleanup"
concern lives in one auditable file.

    §1  prune_system       strip filler/hedges from prose (keeps code)
    §2  derive/payload_extras  complexity → Top-P / penalties
    §3  repair             deterministic AST/JSON fenced-block repair
    §4  decompose/race_tasks  split multi-part prompts, parallel-race them
    §5  RING               short-window provider health ring

Every function is deterministic, pure-stdlib, and never raises.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("axelr.hardening")

__all__ = [
    # §1 pruning
    "prune_system",
    # §2 hyperparams
    "Hyperparams", "derive", "payload_extras",
    # §3 repair
    "ast_repair",
    # §4 task split
    "decompose", "race_tasks", "is_decomposable",
    # §5 telemetry
    "RING", "TelemetryRing", "ProviderEvent",
    # audit
    "audit",
]


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Prompt pruning
# ═══════════════════════════════════════════════════════════════════════════
_FENCE_RE = re.compile(r"```[\s\S]*?```")
_PROSE_FILLER_RE = re.compile(
    r"\b(?:please|kindly|just|really|very|actually|basically|simply|"
    r"maybe|perhaps|you know|like|literally|honestly|frankly|"
    r"if you don'?t mind|if possible|when you can|at your earliest)\b", re.I)
_HEDGE_RE = re.compile(
    r"\b(?:i think|i believe|it seems|it appears|possibly|probably|"
    r"might be|may be|could be|sort of|kind of|more or less)\b", re.I)
_INTENSIFIER_RE = re.compile(
    r"\b(?:very|extremely|incredibly|absolutely|totally|completely|"
    r"utterly|highly|deeply|strongly)\s+", re.I)
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_MULTINL_RE = re.compile(r"\n{3,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")


def prune_system(text: str) -> str:
    """Strip low-info tokens from prose. Preserves code fences verbatim.
    Idempotent: prune(prune(x)) == prune(x)."""
    if not text or not text.strip():
        return text or ""
    fences: list[str] = []

    def _stash(m: re.Match) -> str:
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    stashed = _FENCE_RE.sub(_stash, text)
    stashed = _HEDGE_RE.sub("", stashed)
    stashed = _PROSE_FILLER_RE.sub("", stashed)
    stashed = _INTENSIFIER_RE.sub("", stashed)
    stashed = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", stashed)
    stashed = _MULTISPACE_RE.sub(" ", stashed)
    stashed = _MULTINL_RE.sub("\n\n", stashed)

    def _restore(m: re.Match) -> str:
        idx = int(m.group(1))
        return fences[idx] if 0 <= idx < len(fences) else m.group(0)

    return re.sub(r"\x00FENCE(\d+)\x00", _restore, stashed).strip()


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Hyperparameters
# ═══════════════════════════════════════════════════════════════════════════
_CODE_HINT_RE = re.compile(
    r"```|\bdef\s|\bclass\s|\bfunction\s|\bconst\s|\bSELECT\s|\bINSERT\s")
_PUNCT_RE = re.compile(r"[.,;:!?]")


def _complexity(prompt: str, task_type: str = "", intent: str = "") -> float:
    if not prompt:
        return 0.0
    words = prompt.split()
    n = len(words)
    if n == 0:
        return 0.0
    length_sig = min(1.0, n / 200.0)
    vocab_div = min(1.0, len({w.lower() for w in words}) / n)
    punct_sig = min(1.0, len(_PUNCT_RE.findall(prompt)) / max(1.0, n / 8.0))
    code_sig = 1.0 if _CODE_HINT_RE.search(prompt) else 0.0
    c = 0.35 * length_sig + 0.25 * vocab_div + 0.15 * punct_sig + 0.25 * code_sig
    if task_type in ("extraction", "data") or intent == "extraction":
        c = min(c, 0.25)
    if task_type == "touch_fix":
        c = min(c, 0.45)
    return max(0.0, min(1.0, c))


@dataclass(frozen=True, slots=True)
class Hyperparams:
    top_p: float
    frequency_penalty: float
    presence_penalty: float
    complexity: float

    def to_dict(self) -> dict[str, float]:
        return {
            "top_p": round(self.top_p, 3),
            "frequency_penalty": round(self.frequency_penalty, 3),
            "presence_penalty": round(self.presence_penalty, 3),
            "complexity": round(self.complexity, 3),
        }


def derive(prompt: str, task_type: str = "", intent: str = "") -> Hyperparams:
    """Map complexity → (top_p, freq_pen, presence_pen). Deterministic."""
    c = _complexity(prompt, task_type, intent)
    if task_type in ("extraction", "data") or intent == "extraction":
        return Hyperparams(0.80, 0.0, 0.0, c)
    return Hyperparams(
        top_p=round(0.85 + 0.12 * c, 3),
        frequency_penalty=round(0.05 + 0.35 * c, 3),
        presence_penalty=round(0.00 + 0.20 * c, 3),
        complexity=round(c, 3),
    )


def payload_extras(prompt: str, task_type: str = "",
                   intent: str = "") -> dict[str, Any]:
    """Return only the OpenAI payload extras. Never raises."""
    try:
        hp = derive(prompt, task_type, intent)
        return {"top_p": hp.top_p, "frequency_penalty": hp.frequency_penalty,
                "presence_penalty": hp.presence_penalty}
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# §3 — AST repair (JSON + Python fenced blocks)
# ═══════════════════════════════════════════════════════════════════════════
_FENCE_LANG_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\n([\s\S]*?)```")
_JSON_LANGS = frozenset({"json", "jsonc", "json5"})
_PY_LANGS = frozenset({"python", "py", "python3"})
_BRACKETS = (("{", "}"), ("[", "]"), ("(", ")"))
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")


def _balance(text: str, open_ch: str, close_ch: str) -> tuple[str, int]:
    depth = 0
    in_str = False
    esc = False
    for ch in text:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth = max(0, depth - 1)
    return (text + close_ch * depth, depth) if depth > 0 else (text, 0)


def _repair_json(text: str) -> tuple[str, list[str]]:
    if not text.strip():
        return text, []
    try:
        json.loads(text)
        return text, []
    except Exception:
        pass
    issues: list[str] = []
    out = text
    stripped = _BLOCK_COMMENT_RE.sub("", out)
    stripped = _LINE_COMMENT_RE.sub("", stripped)
    if stripped != out:
        out = stripped; issues.append("stripped_js_comments")
    new = _TRAILING_COMMA_RE.sub(r"\1", out)
    if new != out:
        out = new; issues.append("stripped_trailing_comma")
    for o, c in _BRACKETS:
        out, fixed = _balance(out, o, c)
        if fixed:
            issues.append(f"closed_{o}{c}_x{fixed}")
    try:
        json.loads(out)
        return out, issues
    except Exception:
        return text, []


def _repair_python(text: str) -> tuple[str, list[str]]:
    if not text.strip():
        return text, []
    try:
        ast.parse(text)
        return text, []
    except SyntaxError:
        pass
    except Exception:
        return text, []
    issues: list[str] = []
    out = text
    for o, c in _BRACKETS:
        out, fixed = _balance(out, o, c)
        if fixed:
            issues.append(f"closed_{o}{c}_x{fixed}")
    if not out.endswith("\n"):
        out += "\n"; issues.append("appended_newline")
    try:
        ast.parse(out)
        return out, issues
    except Exception:
        return text, []


def ast_repair(text: str) -> tuple[str, list[str]]:
    """Walk every fenced block, dispatch by language, repair. Never raises."""
    if not text or "```" not in text:
        return text, []
    all_issues: list[str] = []

    def _handle(m: re.Match) -> str:
        lang = (m.group(1) or "").strip().lower()
        body = m.group(2)
        if lang in _JSON_LANGS:
            fixed, issues = _repair_json(body)
        elif lang in _PY_LANGS:
            fixed, issues = _repair_python(body)
        else:
            return m.group(0)
        if issues:
            all_issues.extend(f"{lang}:{i}" for i in issues)
            return f"```{m.group(1)}\n{fixed}```"
        return m.group(0)

    try:
        new_text = _FENCE_LANG_RE.sub(_handle, text)
    except Exception:
        return text, []
    return new_text, all_issues


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Task split (parallel race)
# ═══════════════════════════════════════════════════════════════════════════
RouteFn = Callable[..., Awaitable[dict[str, Any]]]
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$", re.MULTILINE)
_QUESTION_SPLIT_RE = re.compile(r"\?\s+(?=[A-Z])")
_MIN_CLAUSE = 8
_MAX_CLAUSE = 400


def _clean_clause(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().rstrip(";,")


def is_decomposable(prompt: str) -> bool:
    if not prompt or len(prompt) < 60:
        return False
    if len(_BULLET_RE.findall(prompt)) >= 2:
        return True
    parts = _QUESTION_SPLIT_RE.split(prompt)
    return len(parts) >= 2 and all(len(q.strip()) >= 30 for q in parts)


def decompose(prompt: str, max_tasks: int = 3) -> list[str]:
    if not prompt:
        return [prompt]
    max_tasks = max(1, int(max_tasks))
    bullets = _BULLET_RE.findall(prompt)
    cleaned = [_clean_clause(b) for b in bullets
               if _MIN_CLAUSE <= len(b) <= _MAX_CLAUSE]
    if len(cleaned) >= 2:
        return cleaned[:max_tasks]
    parts = _QUESTION_SPLIT_RE.split(prompt)
    parts = [_clean_clause(p) + ("?" if not p.rstrip().endswith("?") else "")
             for p in parts if p.strip()]
    if len(parts) >= 2 and all(len(p) >= 30 for p in parts):
        return parts[:max_tasks]
    return [prompt]


_MERGE_PROMPT = """\
<task_merge>
You received partial answers to sub-questions of one larger request.

Original request:
{prompt}

Sub-answers (from parallel workers):
{subs}

Produce ONE coherent, unified answer. Preserve every fact, identifier,
and code block. Remove duplication. No meta-commentary, no preamble.
</task_merge>"""


async def _one(route_fn: RouteFn, prompt: str, **kw) -> dict[str, Any]:
    try:
        r = await route_fn(**{**kw, "prompt": prompt})
    except Exception as e:
        logger.debug("task_split_subcall_failed error=%s", e)
        return {"success": False, "text": "", "provider": "none"}
    return r if isinstance(r, dict) else {"success": False, "text": "",
                                          "provider": "none"}


async def race_tasks(
    route_fn: RouteFn, *,
    workspace: str, task_type: str, prompt: str,
    history: Any, files: Any,
    max_tokens: int, temp: float, tier: str,
    user: dict | None, context: str, request: Any,
    max_tasks: int = 3, timeout_s: float = 20.0,
) -> dict[str, Any] | None:
    """Fire N concurrent sub-calls + one merge call. Returns None on non-decomposable."""
    if not is_decomposable(prompt):
        return None
    subs = decompose(prompt, max_tasks=max_tasks)
    if len(subs) <= 1:
        return None
    sub_tokens = max(256, max_tokens // max(2, len(subs) + 1))
    tasks = [
        asyncio.create_task(_one(
            route_fn, sub,
            workspace=workspace, task_type=task_type,
            history=None, files=files,
            max_tokens=sub_tokens, temp=temp, tier=tier,
            user=user, context=context, request=request,
        ), name=f"split::{i}")
        for i, sub in enumerate(subs)
    ]
    done, pending = await asyncio.wait(tasks, timeout=max(4.0, float(timeout_s)))
    for t in pending:
        if not t.done():
            t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    collected: list[str] = []
    for sub, t in zip(subs, tasks):
        try:
            r = t.result() if t.done() and not t.cancelled() else None
        except Exception:
            r = None
        if isinstance(r, dict) and r.get("success") and r.get("text"):
            collected.append(f"Q: {sub}\nA: {r['text'].strip()}")
    if len(collected) < 2:
        return None
    subs_block = "\n\n".join(collected)
    merge_prompt = _MERGE_PROMPT.format(
        prompt=prompt[:1500], subs=subs_block[:6000])
    try:
        merged = await asyncio.wait_for(
            route_fn(workspace, task_type, merge_prompt,
                     None, None, max_tokens, temp, tier,
                     user, context, request),
            timeout=max(6.0, float(timeout_s)),
        )
    except Exception as e:
        logger.debug("task_split_merge_failed error=%s", e)
        return None
    if not isinstance(merged, dict) or not merged.get("success"):
        return None
    merged["task_split"] = {"sub_tasks": len(subs), "collected": len(collected)}
    return merged


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Provider telemetry ring (20-slot)
# ═══════════════════════════════════════════════════════════════════════════
_SLOT_SIZE = 20


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    provider: str
    success: bool
    latency_ms: float
    ts: float


class TelemetryRing:
    __slots__ = ("_lock", "_rings", "_size")

    def __init__(self, slot_size: int = _SLOT_SIZE) -> None:
        self._lock = threading.Lock()
        self._rings: dict[tuple[str, str], deque[ProviderEvent]] = {}
        self._size = max(4, int(slot_size))

    def _bucket(self, workspace: str, tier: str) -> deque[ProviderEvent]:
        key = (workspace or "core", tier or "free")
        d = self._rings.get(key)
        if d is None:
            d = deque(maxlen=self._size)
            self._rings[key] = d
        return d

    def record(self, workspace: str, tier: str, provider: str,
               success: bool, latency_ms: float) -> None:
        if not provider:
            return
        try:
            ev = ProviderEvent(provider=provider, success=bool(success),
                               latency_ms=float(latency_ms), ts=time.time())
        except Exception:
            return
        with self._lock:
            self._bucket(workspace, tier).append(ev)

    def _rate(self, d: deque[ProviderEvent], provider: str) -> tuple[float, float]:
        hits = [e for e in d if e.provider == provider]
        if not hits:
            return 0.5, 0.0
        ok = sum(1 for e in hits if e.success)
        return ok / len(hits), sum(e.latency_ms for e in hits) / len(hits)

    def recommend(self, workspace: str, tier: str,
                  candidates: list[str]) -> list[str]:
        if not candidates:
            return candidates
        with self._lock:
            d = self._rings.get((workspace or "core", tier or "free"))
            if not d:
                return list(candidates)
            scored = [((-self._rate(d, p)[0], self._rate(d, p)[1], i), p)
                      for i, p in enumerate(candidates)]
        scored.sort()
        return [p for _, p in scored]

    def snapshot(self, workspace: str, tier: str) -> dict[str, Any]:
        with self._lock:
            d = self._rings.get((workspace or "core", tier or "free"))
            if not d:
                return {"size": 0, "providers": {}}
            providers: dict[str, dict[str, float]] = {}
            for e in d:
                r = providers.setdefault(e.provider,
                                         {"hits": 0, "ok": 0, "lat": 0.0})
                r["hits"] += 1
                r["ok"] += 1 if e.success else 0
                r["lat"] += e.latency_ms
            for r in providers.values():
                if r["hits"]:
                    r["rate"] = round(r["ok"] / r["hits"], 3)
                    r["avg_ms"] = round(r["lat"] / r["hits"], 2)
                r.pop("lat", None)
        return {"size": len(d), "providers": providers}


RING = TelemetryRing()


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "pruning": True,
        "hyperparams": True,
        "ast_repair": True,
        "task_split": True,
        "telemetry_slots": _SLOT_SIZE,
        "telemetry_buckets": len(RING._rings),
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
    }