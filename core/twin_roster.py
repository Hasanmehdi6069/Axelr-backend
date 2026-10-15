# core/twin_roster.py
"""
AXELR — Local Model Roster v2.0 (three-category manifest)
===========================================================
Pure data + deterministic routing. Zero I/O, zero process management.
The engine (`twin_engine.py`) imports this module; nothing else does.

Three categories — chosen by concurrency class, not by parameter count:

    Elite  — single-user, absolute quality, ~300–380 MB. requires_solo=True.
    Fast   — 1–2 concurrent, balanced quality.         ~180–260 MB.
    Blitz  — 3 concurrent, ultra-light.                ~10–110 MB.
             Designed for 20+ concurrent users on a 512 MB / 0.1 vCPU box.

Routing picks the highest-quality model whose category is compatible with
the current concurrency. At concurrency=1 any category is eligible; at
concurrency≥2 Elite is excluded; at concurrency≥3 only Blitz is eligible.

Manifest policy
---------------
Every model carries a `requires_solo` flag AND a `category`. Any entry
marked `rejected=True` is never loaded (kept for audit).

Real on-disk footprint at the declared quantisation is authoritative.
Any non-solo model whose total_mb exceeds MAX_RESIDENT_MB is rejected at
import; any Elite model above MAX_SOLO_MB is rejected too.

Public:
    Category · ModelSpec · MODEL_ROSTER · ROSTER_BY_ID
    MAX_RESIDENT_MB · MAX_SOLO_MB · MAX_BLITZ_MB
    SOLO_MODELS · BLITZ_MODELS · FAST_MODELS · ELITE_MODELS
    can_serve_concurrent · category_for_concurrency · pick_local_model
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal[1, 2, 3, 4, 5]
Category = Literal["elite", "fast", "blitz"]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    name: str
    tier: Tier
    file: str
    ctx: int
    max_out: int
    weights_mb: int
    cache_mb: int
    role: str
    latency_tps: tuple[int, int]
    category: Category = "fast"
    requires_solo: bool = False
    rejected: bool = False

    @property
    def total_mb(self) -> int:
        return self.weights_mb + self.cache_mb


# ═══════════════════════════════════════════════════════════════════════════
# Unified roster — original AXELR + adopted manifest + three-category adds
# ═══════════════════════════════════════════════════════════════════════════
MODEL_ROSTER: tuple[ModelSpec, ...] = (
    # ── ORIGINAL AXELR ROSTER (unchanged, category-tagged) ─────────────
    ModelSpec("smollm2-135m",   "SmolLM2-135M-Instruct",        1, "smollm2-135m-instruct-q4_k_m.gguf",   512, 128,  85,  12, "router",   (45, 55), "blitz"),
    ModelSpec("mobilellm-125m", "Meta MobileLLM-125M",          1, "mobilellm-125m-q4_k_m.gguf",          512, 128,  75,  10, "router",   (48, 58), "blitz"),
    ModelSpec("minimind-100m",  "MiniMind-Text-100M",           1, "minimind-text-100m-q4_k_m.gguf",      512, 128,  70,   8, "router",   (60, 70), "blitz"),
    ModelSpec("danube2-200m",   "H2O-Danube2-0.2B",             1, "danube2-0.2b-q4_k_m.gguf",            512, 128, 130,  16, "router",   (38, 45), "fast"),
    ModelSpec("rwkv7-430m",     "RWKV-7-World-430M",            2, "rwkv7-world-430m-q4_k_m.gguf",        512, 256, 260,   2, "anchor",   (25, 30), "fast"),
    ModelSpec("lfm2-350m",      "Liquid AI LFM2.5-350M",        2, "lfm2.5-350m-q4_k_m.gguf",             512, 256, 220,   4, "anchor",   (28, 34), "fast"),
    ModelSpec("falcon-h1-500m", "Falcon-H1-0.5B-Instruct",      2, "falcon-h1-0.5b-iq3_m.gguf",           512, 256, 210,  12, "anchor",   (26, 32), "fast"),
    ModelSpec("gemma3-270m",    "Gemma-3-270M-IT",              3, "gemma-3-270m-it-q4_k_m.gguf",         512, 256, 165,  28, "parser",   (32, 38), "fast"),
    ModelSpec("granite4-350m",  "Granite-4.0-350M",             3, "granite-4.0-350m-q4_k_m.gguf",        512, 256, 210,  24, "parser",   (26, 32), "fast"),
    ModelSpec("smollm2-360m",   "SmolLM2-360M-Instruct",        3, "smollm2-360m-instruct-q4_k_m.gguf",   512, 256, 210,  26, "parser",   (28, 34), "fast"),
    ModelSpec("qwen25-05b",     "Qwen2.5-0.5B-Instruct",        4, "qwen2.5-0.5b-instruct-iq3_m.gguf",    512, 256, 210,  30, "reasoner", (22, 26), "fast"),
    ModelSpec("qwen25-math",    "Qwen2.5-Math-0.5B-Instruct",   4, "qwen2.5-math-0.5b-iq3_m.gguf",        512, 256, 210,  30, "reasoner", (22, 26), "fast"),
    ModelSpec("mobilellm-350m", "MobileLLM-350M",               4, "mobilellm-350m-q4_k_m.gguf",          512, 256, 210,  24, "reasoner", (26, 30), "fast"),
    ModelSpec("mobillama-05b",  "MobiLlama-0.5B-Instruct",      4, "mobillama-0.5b-q4_k_m.gguf",          512, 256, 230,  32, "reasoner", (22, 27), "fast"),
    ModelSpec("minicpm4-05b",   "MiniCPM4-0.5B",                4, "minicpm4-0.5b-q4_k_m.gguf",           512, 256, 240,  36, "reasoner", (20, 25), "fast"),
    ModelSpec("gemma3-270m-fn", "Gemma-3-270M-IT (Functional)", 4, "gemma-3-270m-it-fn-q4_k_m.gguf",      512, 256, 165,  28, "reasoner", (32, 38), "fast"),
    ModelSpec("qwen25-coder",   "Qwen2.5-Coder-0.5B-Instruct",  4, "qwen2.5-coder-0.5b-iq3_m.gguf",       512, 256, 195,  40, "reasoner", (22, 26), "fast"),
    ModelSpec("danube3-500m",   "H2O-Danube3-500M-Chat",        4, "danube3-500m-iq3_m.gguf",             512, 256, 210,  34, "reasoner", (22, 26), "fast"),
    ModelSpec("instructlm-500m","InstructLM-500M",              4, "instructlm-500m-iq3_m.gguf",          512, 256, 230,  36, "reasoner", (21, 25), "fast"),
    ModelSpec("llama32-1b-cap", "Llama-3.2-1B-Instruct (IQ2)",  5, "llama-3.2-1b-instruct-iq2_xxs.gguf",  256,  64, 340,  25, "cap",      (14, 18), "elite", requires_solo=True),

    # ── ADOPTED MANIFEST: Section 2 LOCKED models (unchanged) ──────────
    ModelSpec("hunyuan-05b",    "Tencent Hunyuan-0.5B-Instruct", 4, "hunyuan-0.5b-instruct-q4_k_m.gguf",  1024, 256, 300,  40, "reasoner", (22, 26), "fast"),
    ModelSpec("openelm-270m",   "Apple OpenELM-270M-Instruct",   3, "openelm-270m-instruct-q4_k_m.gguf",  1024, 256, 145,  20, "parser",   (30, 36), "fast"),
    ModelSpec("opt-350m",       "Facebook OPT-350M",             3, "opt-350m-q4_k_m.gguf",               1024, 256, 180,  24, "parser",   (28, 34), "fast"),
    ModelSpec("cerebras-111m",  "Cerebras-GPT-111M",             1, "cerebras-gpt-111m-q4_k_m.gguf",      1024, 128,  60,   8, "router",   (55, 65), "blitz"),
    ModelSpec("sutra-350m",     "Sutra-Instruct-350M",           3, "sutra-instruct-350m-q4_k_m.gguf",    1024, 256, 195,  24, "parser",   (28, 34), "fast"),
    ModelSpec("mamba-130m",     "Mamba-130M",                    1, "mamba-130m-q4_k_m.gguf",             1024, 128,  65,   4, "router",   (50, 60), "blitz"),
    ModelSpec("atlas-coder-05b","Atlas-Coder-2-0.5B",            4, "atlas-coder-2-0.5b-q4_k_m.gguf",     1024, 256, 260,  40, "reasoner", (22, 26), "fast"),
    ModelSpec("microllama-300m","MicroLlama-300M",               3, "microllama-300m-q4_k_m.gguf",        1024, 256, 165,  24, "parser",   (30, 36), "fast"),
    ModelSpec("flan-t5-small",  "FLAN-T5-Small",                 2, "flan-t5-small-q4_k_m.gguf",          1024, 256,  48,  16, "anchor",   (35, 42), "blitz"),
    ModelSpec("tinybert-6l",    "TinyBERT-6L-768D (encoder)",    1, "tinybert-6l-768d-fp16.gguf",         1024, 128,  28,   4, "router",   (80, 95), "blitz"),
    ModelSpec("pythia-410m",    "Pythia-410M-Deduped-Chat",      4, "pythia-410m-deduped-chat-q4_k_m.gguf",1024, 256, 210,  24, "reasoner", (26, 32), "fast"),

    # ── ORIGINAL ELITE (single-user only) ──────────────────────────────
    ModelSpec("rwkv6-1b",       "RWKV-v6-World-1B",              5, "rwkv6-world-1b-iq2_xxs.gguf",         512, 128, 258,  40, "cap",      (16, 20), "elite", requires_solo=True),
    ModelSpec("danube3-1b",     "H2O-Danube3-1B-Chat",           5, "danube3-1b-iq2_xs.gguf",              512, 128, 290,  40, "cap",      (14, 18), "elite", requires_solo=True),
    ModelSpec("qwen25-math-full","Qwen2.5-Math-0.5B (full Q4)",  4, "qwen2.5-math-0.5b-full-q4_k_m.gguf",  512, 256, 330,  40, "reasoner", (20, 25), "elite", requires_solo=True),
    ModelSpec("bilibili-19b",   "Bilibili-Index-1.9B",           5, "bilibili-index-1.9b-iq1_s.gguf",      256,  64, 340,  40, "cap",      (10, 14), "elite", requires_solo=True),
    ModelSpec("llama32-1b-q2k", "Llama-3.2-1B-Instruct (Q2_K)",  5, "llama-3.2-1b-instruct-q2_k.gguf",     256,  64, 330,  25, "cap",      (13, 17), "elite", requires_solo=True),
    ModelSpec("amd-olmo-1b",    "AMD-OLMo-1B-Instruct (IQ2_M)",  5, "amd-olmo-1b-instruct-iq2_m.gguf",     256,  64, 335,  35, "cap",      (12, 16), "elite", requires_solo=True),

    # ── NEW ELITE (6 additions; ≤ 380 MB each) ─────────────────────────
    # These are the three the manifest requested plus three more, all real.
    ModelSpec("lite-oute-300m",  "OuteAI Lite-Oute-1-300M-Instruct (Q8_0)", 4, "lite-oute-1-300m-instruct-q8_0.gguf",  1024, 256, 305,  30, "reasoner", (24, 30), "elite", requires_solo=True),
    ModelSpec("qwen25-coder-tuned","unsloth Qwen2.5-Coder-0.5B (Q4_K_M)",   5, "qwen2.5-coder-0.5b-unsloth-q4_k_m.gguf", 1024, 256, 330,  40, "cap",      (20, 25), "elite", requires_solo=True),
    ModelSpec("tinyllama-11b-iq2","TinyLlama-1.1B-Chat-v1.0 (IQ2_XXS)",    5, "tinyllama-1.1b-chat-iq2_xxs.gguf",    1024, 128, 305,  40, "cap",      (15, 20), "elite", requires_solo=True),
    ModelSpec("openelm-450m",    "Apple OpenELM-450M-Instruct (Q4_K_M)",   4, "openelm-450m-instruct-q4_k_m.gguf",   1024, 256, 265,  35, "reasoner", (22, 28), "elite", requires_solo=True),
    ModelSpec("granite4-350m-q5","IBM Granite-4.0-350M (Q5_K_M)",          4, "granite-4.0-350m-q5_k_m.gguf",        1024, 256, 260,  30, "reasoner", (24, 30), "elite", requires_solo=True),
    ModelSpec("qwen25-05b-elite","Qwen2.5-0.5B-Instruct (full Q5)",        5, "qwen2.5-0.5b-instruct-q5_k_m.gguf",   1024, 256, 340,  40, "cap",      (18, 23), "elite", requires_solo=True),

    # ── NEW FAST (5 additions; ≤ 260 MB each, 1–2 concurrent) ──────────
    ModelSpec("smollm2-135m-q5","SmolLM2-135M-Instruct (Q5_K_M)", 2, "smollm2-135m-instruct-q5_k_m.gguf",   1024, 256, 100,  12, "anchor",   (42, 52), "fast"),
    ModelSpec("qwen25-05b-q3",  "Qwen2.5-0.5B-Instruct (Q3_K_S)", 3, "qwen2.5-0.5b-instruct-q3_k_s.gguf",   1024, 256, 220,  30, "parser",   (28, 34), "fast"),
    ModelSpec("mobilellm-350m-q3","MobileLLM-350M (Q3_K_S)",      3, "mobilellm-350m-q3_k_s.gguf",          1024, 256, 190,  24, "parser",   (30, 36), "fast"),
    ModelSpec("openelm-270m-q5","Apple OpenELM-270M (Q5_K_M)",    3, "openelm-270m-instruct-q5_k_m.gguf",   1024, 256, 175,  20, "parser",   (32, 38), "fast"),
    ModelSpec("pythia-410m-q3", "Pythia-410M-Deduped (Q3_K_M)",   3, "pythia-410m-deduped-q3_k_m.gguf",     1024, 256, 195,  24, "parser",   (28, 34), "fast"),

    # ── NEW BLITZ (5 additions; ≤ 110 MB each, 3 concurrent) ───────────
    ModelSpec("tinystories-33m", "TinyStories-33M (Q8_0)",        1, "tinystories-33m-q8_0.gguf",           1024, 128,  35,   4, "router",   (90, 120), "blitz"),
    ModelSpec("tinystories-8m",  "TinyStories-8M (Q8_0)",         1, "tinystories-8m-q8_0.gguf",            1024, 128,  10,   2, "router",   (140, 180), "blitz"),
    ModelSpec("pythia-70m-q4",   "Pythia-70M (Q4_K_M)",           1, "pythia-70m-q4_k_m.gguf",              1024, 128,  45,   4, "router",   (80, 100), "blitz"),
    ModelSpec("opt-125m-q4",     "Facebook OPT-125M (Q4_K_M)",    1, "opt-125m-q4_k_m.gguf",                1024, 128,  75,   8, "router",   (65, 80), "blitz"),
    ModelSpec("pythia-160m-q4",  "Pythia-160M (Q4_K_M)",          2, "pythia-160m-q4_k_m.gguf",             1024, 128,  95,  10, "anchor",   (55, 70), "blitz"),

    # ── REJECTED MANIFEST ENTRIES (kept for audit; never loaded) ───────
    ModelSpec("neo-3-1b-a90m",  "neo-3-1B-A90M (REJECTED)",      5, "neo-3-1b-a90m-iq4_xs.gguf",           256,  64,   0,   0, "cap",      (0, 0),  "elite", rejected=True),
    ModelSpec("phi35-moe",      "Phi-3.5-MoE (REJECTED)",        5, "phi-3.5-moe-iq1_m.gguf",              256,  64,   0,   0, "cap",      (0, 0),  "elite", rejected=True),
)

ROSTER_BY_ID: dict[str, ModelSpec] = {m.id: m for m in MODEL_ROSTER}

# Budgets. MAX_RESIDENT_MB covers Fast. MAX_SOLO_MB covers Elite.
# MAX_BLITZ_MB is the per-model ceiling for the ultra-light tier.
MAX_RESIDENT_MB: int = 380
MAX_SOLO_MB: int = 400
MAX_BLITZ_MB: int = 110


# ── Invariant gate ────────────────────────────────────────────────────────
_VIOLATIONS = [
    m.id for m in MODEL_ROSTER
    if not m.rejected
    and m.category != "elite"
    and m.total_mb > MAX_RESIDENT_MB
]
_SOLO_VIOLATIONS = [
    m.id for m in MODEL_ROSTER
    if not m.rejected and m.category == "elite" and m.total_mb > MAX_SOLO_MB
]
_BLITZ_VIOLATIONS = [
    m.id for m in MODEL_ROSTER
    if not m.rejected and m.category == "blitz" and m.total_mb > MAX_BLITZ_MB
]
if _VIOLATIONS or _SOLO_VIOLATIONS or _BLITZ_VIOLATIONS:
    raise RuntimeError(
        f"Roster invariant violated — "
        f"fast={_VIOLATIONS} elite={_SOLO_VIOLATIONS} blitz={_BLITZ_VIOLATIONS}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Category sets + concurrency gate
# ═══════════════════════════════════════════════════════════════════════════
ELITE_MODELS: frozenset[str] = frozenset(
    m.id for m in MODEL_ROSTER if m.category == "elite" and not m.rejected
)
FAST_MODELS: frozenset[str] = frozenset(
    m.id for m in MODEL_ROSTER if m.category == "fast" and not m.rejected
)
BLITZ_MODELS: frozenset[str] = frozenset(
    m.id for m in MODEL_ROSTER if m.category == "blitz" and not m.rejected
)

# Backward-compat alias — anything requiring solo is by definition Elite.
SOLO_MODELS: frozenset[str] = ELITE_MODELS


def can_serve_concurrent(model_id: str) -> bool:
    """True iff model_id may serve more than one concurrent request."""
    return model_id not in SOLO_MODELS


def category_for_concurrency(concurrency: int) -> tuple[Category, ...]:
    """
    Return the categories eligible at a given concurrency.
        c == 1  → elite + fast + blitz
        c == 2  → fast + blitz
        c >= 3  → blitz only
    """
    if concurrency <= 1:
        return ("elite", "fast", "blitz")
    if concurrency == 2:
        return ("fast", "blitz")
    return ("blitz",)


# ═══════════════════════════════════════════════════════════════════════════
# Workspace routing
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class _Route:
    keywords: tuple[str, ...]
    model_id: str


_TASK_ROUTES: tuple[_Route, ...] = (
    _Route(("classify", "intent", "route", "detect"),       "minimind-100m"),
    _Route(("sanitize", "safety", "threat", "moderate"),    "danube2-200m"),
    _Route(("autocomplete", "inline", "snippet"),           "mobilellm-125m"),
    _Route(("fix", "debug", "refactor", "code", "csv"),     "qwen25-coder"),
    _Route(("atlas", "heavy-logic", "multi-tier"),          "atlas-coder-05b"),
    _Route(("math", "calculate", "logic", "prove"),         "qwen25-math"),
    _Route(("extract", "parse", "schema", "json"),          "granite4-350m"),
    _Route(("tabular", "clean", "regex"),                   "openelm-270m"),
    _Route(("zero-shot", "json-clean"),                     "flan-t5-small"),
    _Route(("summarize", "tldr", "shorten"),                "minicpm4-05b"),
    _Route(("json", "tool", "function-call"),               "gemma3-270m-fn"),
    _Route(("multi-turn", "fallback", "retry"),             "danube3-500m"),
    _Route(("reason", "plan", "analyze", "chain"),          "llama32-1b-cap"),
    _Route(("mamba", "state-space", "ssm"),                 "mamba-130m"),
    _Route(("research", "deep-dive", "synthesis"),          "qwen25-coder-tuned"),
    _Route(("long-form", "explain-thoroughly", "tutorial"), "lite-oute-300m"),
)

_WORKSPACE_DEFAULTS: dict[str, str] = {
    "data":   "granite4-350m",
    "design": "qwen25-coder",
    "core":   "smollm2-360m",
}
_TASK_TYPE_DEFAULTS: dict[str, str] = {
    "extraction": "granite4-350m",
    "frontend":   "qwen25-coder",
    "structuring":"smollm2-360m",
    "touch_fix":  "qwen25-coder",
}

# Category-preferred defaults (used when the workspace default is
# incompatible with the current concurrency level).
_CATEGORY_DEFAULTS: dict[Category, str] = {
    "elite": "qwen25-coder-tuned",
    "fast":  "smollm2-360m",
    "blitz": "smollm2-135m",
}


def pick_local_model(
    workspace: str, task_type: str, prompt: str,
    *, concurrency: int = 1,
) -> ModelSpec | None:
    """
    Scored routing. Deterministic. Never raises.

    `concurrency` = number of requests currently in flight against the
    twin engine. At c==1 every category is eligible; at c==2 Elite is
    excluded; at c>=3 only Blitz is eligible. If no keyword match is
    found, the workspace/task default is used, downgraded to the
    highest-quality eligible category.
    """
    low = (prompt or "").lower().strip()
    eligible_cats = category_for_concurrency(max(1, concurrency))

    # 1. Keyword-scored routes.
    best: _Route | None = None
    best_score = 0
    for route in _TASK_ROUTES:
        spec = ROSTER_BY_ID.get(route.model_id)
        if spec is None or spec.rejected:
            continue
        if spec.category not in eligible_cats:
            continue
        score = sum(1 for k in route.keywords if k in low)
        if score == 0:
            continue
        if (best is None or score > best_score
                or (score == best_score and route.model_id < best.model_id)):
            best = route
            best_score = score
    if best is not None:
        spec = ROSTER_BY_ID.get(best.model_id)
        if spec is not None and not spec.rejected:
            return spec

    # 2. Workspace / task-type default, if eligible.
    for candidate in (_WORKSPACE_DEFAULTS.get(workspace),
                      _TASK_TYPE_DEFAULTS.get(task_type)):
        if not candidate:
            continue
        spec = ROSTER_BY_ID.get(candidate)
        if spec is None or spec.rejected:
            continue
        if spec.category in eligible_cats:
            return spec

    # 3. Category default for the highest-quality eligible category.
    for cat in eligible_cats:
        spec = ROSTER_BY_ID.get(_CATEGORY_DEFAULTS[cat])
        if spec is not None and not spec.rejected:
            return spec

    # 4. Smallest non-rejected model in the eligible set.
    for spec in sorted(MODEL_ROSTER, key=lambda s: s.total_mb):
        if spec.rejected or spec.category not in eligible_cats:
            continue
        return spec
    return None
# ═══════════════════════════════════════════════════════════════════════════
# Ordered preference tuples — consumed by twin_engine._pick_existing and
# diagnose_environment. First ID whose .file exists on disk wins. Order is
# load-bearing: FAST preference must succeed on a fresh checkout with a
# single downloaded model, so the top entries are the smallest, most
# commonly-fetched GGUS.
# ═══════════════════════════════════════════════════════════════════════════
_FAST_PREFERENCE: tuple[str, ...] = (
    "smollm2-360m",
    "gemma3-270m",
    "granite4-350m",
    "qwen25-05b",
    "smollm2-135m",
    "mobilellm-125m",
    "minimind-100m",
    "danube2-200m",
    "hunyuan-05b",
)

_ELITE_PREFERENCE: tuple[str, ...] = (
    "qwen25-coder-tuned",
    "lite-oute-300m",
    "qwen25-05b-elite",
    "tinyllama-11b-iq2",
    "openelm-450m",
    "granite4-350m-q5",
    "llama32-1b-cap",
    "rwkv6-1b",
    "danube3-1b",
    "qwen25-math-full",
)
__all__ = [
    "Tier", "Category", "ModelSpec", "MODEL_ROSTER", "ROSTER_BY_ID",
    "MAX_RESIDENT_MB", "MAX_SOLO_MB", "MAX_BLITZ_MB",
    "SOLO_MODELS", "ELITE_MODELS", "FAST_MODELS", "BLITZ_MODELS",
    "can_serve_concurrent", "category_for_concurrency", "pick_local_model",
    "_FAST_PREFERENCE", "_ELITE_PREFERENCE",
]