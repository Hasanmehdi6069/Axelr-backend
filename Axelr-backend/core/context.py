# core/context.py
"""
AXELR — Context Assembly (intent · window · vault)
====================================================
Everything that answers "what goes in the prompt?" in one file:

    §1 Intent tree      classify(prompt) -> (intent, sub_intent)
    §2 Context window   clip_history(history, max_tokens)
    §3 Prompt vault     build(workspace, intent, prompt) -> directives
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Intent tree
# ═══════════════════════════════════════════════════════════════════════════
_CLAMP = 2000
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\-\s]")

_TOP_KEYWORDS: dict[str, str] = {
    "hi": "conversational", "hello": "conversational", "hey": "conversational",
    "yo": "conversational", "sup": "conversational", "howdy": "conversational",
    "thanks": "conversational", "thank": "conversational", "thx": "conversational",
    "cheers": "conversational", "bye": "conversational", "goodbye": "conversational",
    "build": "transactional", "create": "transactional", "write": "transactional",
    "generate": "transactional", "implement": "transactional", "code": "transactional",
    "develop": "transactional", "scaffold": "transactional", "fix": "transactional",
    "debug": "transactional", "refactor": "transactional", "optimi": "transactional",
    "migrate": "transactional", "deploy": "transactional", "convert": "transactional",
    "render": "transactional", "compile": "transactional",
    "extract": "extraction", "parse": "extraction", "scrape": "extraction",
    "summarise": "extraction", "summarize": "extraction", "classify": "extraction",
    "categorise": "extraction", "categorize": "extraction",
    "style": "styling", "redesign": "styling", "theme": "styling",
    "layout": "styling", "polish": "styling", "beautify": "styling",
    "modernize": "styling", "modernise": "styling",
    "explain": "strategy", "compare": "strategy", "why": "strategy",
    "what": "strategy", "how": "strategy", "should": "strategy",
    "recommend": "strategy", "suggest": "strategy", "advise": "strategy",
    "plan": "strategy", "roadmap": "strategy", "approach": "strategy",
    "error": "diagnostic", "exception": "diagnostic", "traceback": "diagnostic",
    "crash": "diagnostic", "failed": "diagnostic", "broken": "diagnostic",
    "bug": "diagnostic", "issue": "diagnostic",
}

_SUB_PATTERNS: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    "conversational": (
        (re.compile(r"^\s*(hi|hello|hey|yo|sup|howdy)\b", re.I), "greeting"),
        (re.compile(r"^\s*(thanks|thank you|thx|ty|cheers)\b", re.I), "thanks"),
        (re.compile(r"^\s*(ok|okay|cool|nice|great|awesome|got it)\b", re.I), "ack"),
        (re.compile(r"^\s*(bye|goodbye|see you|later|cya)\b", re.I), "farewell"),
        (re.compile(r"\b(how are you|how's it going|what's up)\b", re.I), "smalltalk"),
    ),
    "transactional": (
        (re.compile(r"\b(build|create|scaffold)\b.*\b(app|site|web|ui|component|page)\b", re.I), "build_ui"),
        (re.compile(r"\b(api|endpoint|route|handler|controller|server)\b", re.I), "build_api"),
        (re.compile(r"\b(fix|debug|patch)\b", re.I), "fix"),
        (re.compile(r"\b(refactor|restructure|clean up|simplify)\b", re.I), "refactor"),
        (re.compile(r"\b(optimi[sz]e|speed up|faster)\b", re.I), "optimize"),
        (re.compile(r"\b(test|spec|unit test)\b", re.I), "test"),
        (re.compile(r"\b(sql|database|schema|migration)\b", re.I), "database"),
    ),
    "extraction": (
        (re.compile(r"\b(csv|excel|xlsx|spreadsheet)\b", re.I), "tabular"),
        (re.compile(r"\b(pdf|document|docx)\b", re.I), "document"),
        (re.compile(r"\b(json|xml|yaml|toml)\b", re.I), "structured"),
        (re.compile(r"\b(extract|parse|scrape)\b", re.I), "generic"),
    ),
    "styling": (
        (re.compile(r"\b(dark|light)\s*mode\b", re.I), "theme_swap"),
        (re.compile(r"\b(tailwind|bootstrap|material)\b", re.I), "framework_swap"),
        (re.compile(r"\b(modern|2025|clean|sleek|polished)\b", re.I), "modernize"),
        (re.compile(r"\b(responsive|mobile|tablet|desktop)\b", re.I), "responsive"),
        (re.compile(r"\b(anim|transition|motion)\b", re.I), "animation"),
    ),
    "strategy": (
        (re.compile(r"\b(compare|versus|vs\.?|difference between)\b", re.I), "compare"),
        (re.compile(r"\b(why|reason|cause)\b", re.I), "why"),
        (re.compile(r"\b(how (should|do|can|would|might))\b", re.I), "howto"),
        (re.compile(r"\b(architecture|design pattern|system design|trade[- ]?off)\b", re.I), "architecture"),
        (re.compile(r"\b(plan|roadmap|blueprint|methodology)\b", re.I), "plan"),
        (re.compile(r"\b(explain|what is|what are|define)\b", re.I), "explain"),
    ),
    "diagnostic": (
        (re.compile(r"\b(traceback|stack trace|exception)\b", re.I), "stacktrace"),
        (re.compile(r"\b(500|502|503|504)\b"), "http_5xx"),
        (re.compile(r"\b(memory|oom|out of memory|heap)\b", re.I), "memory"),
        (re.compile(r"\b(timeout|hang|stuck|freeze)\b", re.I), "timeout"),
        (re.compile(r"\b(build|compile|lint) (error|fail)", re.I), "build"),
    ),
}


@dataclass(frozen=True, slots=True)
class IntentResult:
    intent: str
    sub_intent: str
    confidence: float
    matched_token: str
    signals: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {"intent": self.intent, "sub_intent": self.sub_intent,
                "confidence": round(self.confidence, 3),
                "matched": self.matched_token}


def _normalize(prompt: str) -> str:
    if not prompt: return ""
    clamped = prompt[:_CLAMP]
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", clamped)).strip()


def classify(prompt: str) -> IntentResult:
    text = _normalize(prompt)
    if not text:
        return IntentResult("conversational", "empty", 0.0, "", ())
    low = text.lower()
    words = _NON_ALNUM_RE.sub(" ", low).split()
    if not words:
        return IntentResult("conversational", "nonlexical", 0.0, "", ())
    best_intent = ""
    best_score = -1
    best_token = ""
    for idx, tok in enumerate(words):
        for prefix, intent in _TOP_KEYWORDS.items():
            if tok == prefix or tok.startswith(prefix):
                score = len(prefix) * 10 - idx
                if score > best_score:
                    best_score = score
                    best_intent = intent
                    best_token = tok
    if not best_intent:
        if len(words) <= 7:
            return IntentResult("conversational", "short", 0.35, "", ())
        return IntentResult("strategy", "unspecified", 0.30, "", ())
    best_sub = "generic"
    signals: list[str] = []
    for pat, sub in _SUB_PATTERNS.get(best_intent, ()):
        m = pat.search(text)
        if m:
            best_sub = sub
            signals.append(m.group(0)[:40])
            break
    base = 0.55 + min(0.30, best_score / 100.0)
    if best_sub != "generic":
        base += 0.10
    return IntentResult(best_intent, best_sub, round(max(0.0, min(1.0, base)), 3),
                        best_token, tuple(signals))


def classify_flat(prompt: str) -> str:
    return classify(prompt).intent


class IntentRouter:
    __slots__ = ()

    async def classify(self, prompt: str = "",
                       files: Iterable[dict] | None = None) -> IntentResult:
        return classify(prompt)


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Context window
# ═══════════════════════════════════════════════════════════════════════════
CHARS_PER_TOKEN = 4
DEFAULT_MAX_TOKENS = 3000
DEFAULT_KEEP_RECENT = 6
DEFAULT_CODE_FENCE_CAP = 1200

_FENCE_OPEN_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n")
_MULTISPACE_RE = re.compile(r"[ \t]+")
_MULTINEWLINE_RE = re.compile(r"\n{3,}")

_FILLERS = frozenset({
    "please", "kindly", "just", "really", "very", "actually", "basically",
    "simply", "maybe", "perhaps", "sure", "okay", "ok", "well", "anyway",
    "so", "thus", "hence", "therefore",
})
_NEGATIONS = frozenset({
    "no", "not", "never", "don't", "dont", "won't", "wont", "can't", "cant",
    "would", "wouldn't", "could", "couldn't", "should", "shouldn't",
    "might", "must", "may", "will", "shall",
})


def estimate_tokens(text: str) -> int:
    if not text: return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass(slots=True)
class ClipResult:
    messages: list[dict[str, Any]]
    dropped: int
    tokens_used: int
    truncated: bool


def _strip_fillers(text: str) -> str:
    if not text: return ""
    text = _MULTISPACE_RE.sub(" ", text)
    words = text.split()
    kept = [w for w in words if w.lower() not in _FILLERS or w.lower() in _NEGATIONS]
    return " ".join(kept) if kept else text


def _shrink_code_blocks(text: str, cap: int = DEFAULT_CODE_FENCE_CAP) -> str:
    if "```" not in text or len(text) <= cap: return text
    out: list[str] = []
    pos = 0
    while True:
        idx = text.find("```", pos)
        if idx == -1:
            out.append(text[pos:]); break
        out.append(text[pos:idx])
        m = _FENCE_OPEN_RE.match(text, idx)
        opener_end = m.end() if m else idx + 3
        close = text.find("```", opener_end)
        if close == -1:
            body = text[opener_end:]
            out.append(text[idx:opener_end])
            out.append(body[:cap] + ("\n…[truncated]\n" if len(body) > cap else ""))
            break
        body = text[opener_end:close]
        if len(body) <= cap:
            out.append(text[idx:close + 3])
        else:
            out.append(text[idx:opener_end] + body[:cap] + "\n…[truncated]\n```")
        pos = close + 3
    return "".join(out)


def _normalise_turn(msg: dict[str, Any]) -> dict[str, Any]:
    role = str(msg.get("role") or "user")
    raw = msg.get("text") or msg.get("content") or ""
    if isinstance(raw, list):
        raw = "\n".join(p.get("text", "") for p in raw if isinstance(p, dict))
    return {"role": role, "text": str(raw)}


def clip_history(
    history: Iterable[dict[str, Any]] | None,
    *, max_tokens: int = DEFAULT_MAX_TOKENS,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    system_anchor: str = "",
    aggressive: bool = True,
) -> ClipResult:
    if not history:
        return ClipResult([], 0, estimate_tokens(system_anchor), False)
    turns = [_normalise_turn(m) for m in history if isinstance(m, dict)]
    if not turns:
        return ClipResult([], 0, estimate_tokens(system_anchor), False)
    budget = max(64, int(max_tokens) - estimate_tokens(system_anchor))
    n = len(turns)
    recent_start = max(0, n - max(1, int(keep_recent)))
    processed: list[dict[str, Any]] = []
    for i, t in enumerate(turns):
        if i >= recent_start:
            processed.append(t)
        elif aggressive:
            processed.append({"role": t["role"],
                              "text": _shrink_code_blocks(_strip_fillers(t["text"]))})
        else:
            processed.append(t)
    kept_rev: list[dict[str, Any]] = []
    used = 0
    dropped = 0
    for i in range(len(processed) - 1, -1, -1):
        cost = estimate_tokens(processed[i]["text"])
        if kept_rev and used + cost > budget:
            dropped = i + 1
            break
        kept_rev.append(processed[i])
        used += cost
    kept_rev.reverse()
    truncated = dropped > 0 or len(kept_rev) < len(processed)
    if dropped and aggressive and kept_rev:
        kept_rev.insert(0, {"role": "system",
                            "text": f"[{dropped} earlier turn(s) omitted for context budget]"})
    if system_anchor:
        kept_rev.insert(0, {"role": "system", "text": system_anchor})
    return ClipResult(kept_rev, dropped, used + estimate_tokens(system_anchor), truncated)


def clip_prompt(prompt: str, *, max_tokens: int = 2000) -> tuple[str, bool]:
    if not prompt: return "", False
    if estimate_tokens(prompt) <= max_tokens: return prompt, False
    cap = max_tokens * CHARS_PER_TOKEN
    return prompt[:cap].rstrip() + "\n…[prompt clipped]", True


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Prompt vault
# ═══════════════════════════════════════════════════════════════════════════
_THINKING_VARIANTS: tuple[str, ...] = (
    "<reasoning_protocol>\nWrap internal analysis in <thinking>...</thinking>.\n"
    "Labels inside: INTENT | WORKSPACE | VETO.\n"
    "Close the tag before the visible reply begins.\n</reasoning_protocol>",
    "<reasoning_protocol>\n1. Emit <thinking>.\n"
    "2. Inside: state INTENT, WORKSPACE, VETO (active|inactive).\n"
    "3. Emit </thinking>.\n4. Emit visible reply.\n</reasoning_protocol>",
    "<reasoning_protocol mode=\"compact\">\n"
    "<thinking>INTENT=? WORKSPACE=? VETO=?</thinking>\n"
    "Then visible reply only.\n</reasoning_protocol>",
)

_INTENT_VAULT: dict[str, tuple[str, ...]] = {
    "conversational": (
        "<intent_veto mode=\"conversational\">\nReply in 1-2 polished human sentences.\n"
        "Forbidden: code blocks, dashboards, mock data, feature bullet lists,\n"
        "'let me know if...' trailers. Answer like a person.\n</intent_veto>",
        "<intent_veto mode=\"conversational\">\nOne short paragraph. "
        "No markdown headings. No code. No lists.\n</intent_veto>",
        "<intent_veto mode=\"conversational\" density=\"tight\">\n"
        "Human voice. ≤ 60 words. Zero scaffolding.\n</intent_veto>",
    ),
    "transactional": (
        "<transactional_directive>\nDeliver runnable output.\n"
        "* Single code block, language-tagged.\n"
        "* Inline comments only where behaviour is non-obvious.\n"
        "* No explanatory prose above the block.\n"
        "* No 'here is your code' preamble.\n</transactional_directive>",
        "<transactional_directive mode=\"surgical\">\nReturn the artefact. "
        "Not a description of the artefact.\n"
        "One fenced block. Language-tagged. Zero narrative.\n</transactional_directive>",
        "<transactional_directive mode=\"checklist\">\n1. Fenced code block, correct language tag.\n"
        "2. No prose above.\n"
        "3. One-line note only if a non-obvious choice was made.\n</transactional_directive>",
    ),
    "extraction": (
        "<extraction_directive>\nReturn only the requested field(s). "
        "Format as a compact JSON array\ninside ```json fences. "
        "No commentary, no schema, no explanation.\n</extraction_directive>",
        "<extraction_directive mode=\"strict\">\n"
        "Output shape: ```json\\n[...]\\n```. Only the array. Nothing else.\n</extraction_directive>",
        "<extraction_directive mode=\"tabular\">\n"
        "Emit a markdown table with one row per record. No preamble.\n</extraction_directive>",
    ),
    "styling": (
        "<styling_directive era=\"2025\">\nModern SaaS surface. System font stack. "
        "Rounded corners on\ninteractive elements. Surface hierarchy via shadows — not 1px\n"
        "solid borders. Generous spacing (p-4+). Flex/grid — never <table>\n"
        "for layout. Coherent palette (slate/zinc/neutral + one accent).\n</styling_directive>",
        "<styling_directive era=\"2025\" density=\"high\">\n"
        "Tailwind-first. No Arial/Helvetica hardcodes. Rounded-lg+.\n"
        "Shadow-based hierarchy. p-4 minimum, py-8+ on sections.\n"
        "NEVER: `border: 1px solid #ccc`, `<table>` layout, flat white bg.\n</styling_directive>",
        "<styling_directive era=\"2025\" mode=\"checklist\">\n"
        "□ font-sans via system stack\n□ rounded-lg on buttons/cards\n"
        "□ shadow-sm/md on surfaces\n□ flex/grid layout only\n"
        "□ coherent palette, one accent\n□ p-4+ padding, py-8+ on sections\n</styling_directive>",
    ),
    "strategy": (
        "<strategy_directive>\nClean conceptual blueprint with actionable structure. "
        "No code\nblocks unless explicitly requested. Never demand a dataset — if\n"
        "data is missing, describe the shape or schema in the abstract.\n</strategy_directive>",
        "<strategy_directive mode=\"abstract\">\nFrameworks, trade-offs, decision criteria. "
        "Illustrative, not\nimplementational. Never gate on missing input.\n</strategy_directive>",
        "<strategy_directive mode=\"numbered\">\n"
        "Numbered blueprint. Each step: action + rationale (one line).\n"
        "No code. No 'I would need…' stalls.\n</strategy_directive>",
    ),
    "diagnostic": (
        "<diagnostic_directive>\nRoot cause first. Then the minimal fix. Then one guard.\n"
        "No 'have you tried' hedging. No 5-step debugging lecture.\n</diagnostic_directive>",
        "<diagnostic_directive mode=\"rca\">\n1. Cause (one sentence).\n"
        "2. Fix (fenced code, language-tagged).\n"
        "3. Guard (one line — test, assertion, or check).\n</diagnostic_directive>",
        "<diagnostic_directive mode=\"triage\">\n"
        "State cause. Emit patch. Stop. No apologising.\n</diagnostic_directive>",
    ),
}

_DRIFT_VARIANTS: tuple[str, ...] = (
    "<data_drift_defiance>\nNever halt because data is missing. Never demand a dataset.\n"
    "When input is absent, produce a self-sustaining structural\n"
    "blueprint and mark any synthetic values as illustrative.\n</data_drift_defiance>",
    "<data_drift_defiance mode=\"strict\">\nMissing input ⇒ synthesise a plausible shape. "
    "Never ask the user\nto upload more data before proceeding.\n</data_drift_defiance>",
    "<data_drift_defiance mode=\"terse\">\n"
    "No dataset? Invent a representative shape. Proceed.\n</data_drift_defiance>",
)

_WORKSPACE_ANCHOR: dict[str, str] = {
    "data":   "<workspace_anchor>DATA: tables, extraction, metrics, pipelines.</workspace_anchor>",
    "design": "<workspace_anchor>DESIGN: UI, components, layouts, styling.</workspace_anchor>",
    "core":   "<workspace_anchor>CORE: reasoning, drafting, general assistance.</workspace_anchor>",
}


def _pick(variants: tuple[str, ...], seed: int) -> str:
    if not variants: return ""
    return variants[seed % len(variants)]


def _seed(workspace: str, prompt: str) -> int:
    h = hashlib.blake2b(f"{workspace}|{prompt[:200]}".encode("utf-8", errors="ignore"),
                        digest_size=4).digest()
    return int.from_bytes(h, "big")


def build(workspace: str, intent: str, sub_intent: str = "",
          *, prompt: str = "", include_drift: bool = True) -> str:
    try:
        seed = _seed(workspace or "core", prompt or "")
        parts: list[str] = []
        anchor = _WORKSPACE_ANCHOR.get(workspace or "core")
        if anchor:
            parts.append(anchor)
        parts.append(_pick(_THINKING_VARIANTS, seed))
        slot = _INTENT_VAULT.get(intent or "strategy")
        if slot:
            parts.append(_pick(slot, seed >> 3))
        if include_drift:
            parts.append(_pick(_DRIFT_VARIANTS, seed >> 5))
        return "\n\n".join(p for p in parts if p)
    except Exception:
        return _THINKING_VARIANTS[0]


def install_into_prompts() -> bool:
    try:
        from api import prompts as P
    except Exception:
        return False
    addenda = getattr(P, "_WORKSPACE_ADDENDA", None)
    if not isinstance(addenda, dict):
        return False
    if addenda.get("vault_installed"):
        return True
    for ws in ("data", "design", "core"):
        directive = build(ws, "generic", prompt="")
        base = addenda.get(ws, "")
        if directive and directive not in base:
            addenda[ws] = base + "\n\n" + directive
    addenda["vault_installed"] = True
    return True


__all__ = [
    # intent
    "IntentResult", "classify", "classify_flat", "IntentRouter",
    # window
    "CHARS_PER_TOKEN", "DEFAULT_MAX_TOKENS", "DEFAULT_KEEP_RECENT",
    "ClipResult", "estimate_tokens", "clip_history", "clip_prompt",
    # vault
    "build", "install_into_prompts",
]