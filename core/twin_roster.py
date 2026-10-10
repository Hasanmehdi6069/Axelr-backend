# core/twin_roster.py
"""
AXELR — Local Model Roster (20 GGUF specs + scored routing)
=============================================================
Pure data + deterministic routing. Zero I/O, zero process management.
The engine (`twin_engine.py`) imports this module; nothing else does.

Public:
    ModelSpec · MODEL_ROSTER · ROSTER_BY_ID · MAX_RESIDENT_MB · pick_local_model
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal[1, 2, 3, 4, 5]


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
    role: str                        # router | anchor | parser | reasoner | cap
    latency_tps: tuple[int, int]

    @property
    def total_mb(self) -> int:
        return self.weights_mb + self.cache_mb


MODEL_ROSTER: tuple[ModelSpec, ...] = (
    ModelSpec("smollm2-135m",   "SmolLM2-135M-Instruct",       1, "smollm2-135m-instruct-q4_k_m.gguf",   512, 128, 85,  12, "router",   (45, 55)),
    ModelSpec("mobilellm-125m", "Meta MobileLLM-125M",         1, "mobilellm-125m-q4_k_m.gguf",          512, 128, 75,  10, "router",   (48, 58)),
    ModelSpec("minimind-100m",  "MiniMind-Text-100M",          1, "minimind-text-100m-q4_k_m.gguf",      512, 128, 70,   8, "router",   (60, 70)),
    ModelSpec("danube2-200m",   "H2O-Danube2-0.2B",            1, "danube2-0.2b-q4_k_m.gguf",            512, 128, 130, 16, "router",   (38, 45)),
    ModelSpec("rwkv7-430m",     "RWKV-7-World-430M",           2, "rwkv7-world-430m-q4_k_m.gguf",        512, 256, 260,  2, "anchor",   (25, 30)),
    ModelSpec("lfm2-350m",      "Liquid AI LFM2.5-350M",       2, "lfm2.5-350m-q4_k_m.gguf",             512, 256, 220,  4, "anchor",   (28, 34)),
    ModelSpec("falcon-h1-500m", "Falcon-H1-0.5B-Instruct",     2, "falcon-h1-0.5b-iq3_m.gguf",           512, 256, 210, 12, "anchor",   (26, 32)),
    ModelSpec("gemma3-270m",    "Gemma-3-270M-IT",             3, "gemma-3-270m-it-q4_k_m.gguf",         512, 256, 165, 28, "parser",   (32, 38)),
    ModelSpec("granite4-350m",  "Granite-4.0-350M",            3, "granite-4.0-350m-q4_k_m.gguf",        512, 256, 210, 24, "parser",   (26, 32)),
    ModelSpec("smollm2-360m",   "SmolLM2-360M-Instruct",       3, "smollm2-360m-instruct-q4_k_m.gguf",   512, 256, 210, 26, "parser",   (28, 34)),
    ModelSpec("qwen25-05b",     "Qwen2.5-0.5B-Instruct",       4, "qwen2.5-0.5b-instruct-iq3_m.gguf",    512, 256, 210, 30, "reasoner", (22, 26)),
    ModelSpec("qwen25-math",    "Qwen2.5-Math-0.5B-Instruct",  4, "qwen2.5-math-0.5b-iq3_m.gguf",        512, 256, 210, 30, "reasoner", (22, 26)),
    ModelSpec("mobilellm-350m", "MobileLLM-350M",              4, "mobilellm-350m-q4_k_m.gguf",          512, 256, 210, 24, "reasoner", (26, 30)),
    ModelSpec("mobillama-05b",  "MobiLlama-0.5B-Instruct",     4, "mobillama-0.5b-q4_k_m.gguf",          512, 256, 230, 32, "reasoner", (22, 27)),
    ModelSpec("minicpm4-05b",   "MiniCPM4-0.5B",               4, "minicpm4-0.5b-q4_k_m.gguf",           512, 256, 240, 36, "reasoner", (20, 25)),
    ModelSpec("gemma3-270m-fn", "Gemma-3-270M-IT (Functional)",4, "gemma-3-270m-it-fn-q4_k_m.gguf",      512, 256, 165, 28, "reasoner", (32, 38)),
    ModelSpec("qwen25-coder",   "Qwen2.5-Coder-0.5B-Instruct", 4, "qwen2.5-coder-0.5b-iq3_m.gguf",       512, 256, 195, 40, "reasoner", (22, 26)),
    ModelSpec("danube3-500m",   "H2O-Danube3-500M-Chat",       4, "danube3-500m-iq3_m.gguf",             512, 256, 210, 34, "reasoner", (22, 26)),
    ModelSpec("instructlm-500m","InstructLM-500M",             4, "instructlm-500m-iq3_m.gguf",          512, 256, 230, 36, "reasoner", (21, 25)),
    ModelSpec("llama32-1b-cap", "Llama-3.2-1B-Instruct (IQ2)", 5, "llama-3.2-1b-instruct-iq2_xxs.gguf",  256,  64, 340, 25, "cap",      (14, 18)),
)

ROSTER_BY_ID: dict[str, ModelSpec] = {m.id: m for m in MODEL_ROSTER}
if len(MODEL_ROSTER) != 20:
    raise RuntimeError(f"roster must be exactly 20 models, got {len(MODEL_ROSTER)}")

MAX_RESIDENT_MB = 380
for _m in MODEL_ROSTER:
    if _m.total_mb > MAX_RESIDENT_MB:
        raise RuntimeError(f"{_m.id} exceeds budget: {_m.total_mb}MB > {MAX_RESIDENT_MB}MB")

_FAST_PREFERENCE: tuple[str, ...] = (
    "smollm2-360m", "qwen25-05b", "granite4-350m", "smollm2-135m",
)
_ELITE_PREFERENCE: tuple[str, ...] = (
    "llama32-1b-cap", "qwen25-05b", "minicpm4-05b", "danube3-500m",
)


@dataclass(frozen=True, slots=True)
class _Route:
    keywords: tuple[str, ...]
    model_id: str


_TASK_ROUTES: tuple[_Route, ...] = (
    _Route(("classify", "intent", "route", "detect"),      "minimind-100m"),
    _Route(("sanitize", "safety", "threat", "moderate"),   "danube2-200m"),
    _Route(("fix", "debug", "refactor", "code", "csv"),    "qwen25-coder"),
    _Route(("math", "calculate", "logic", "prove"),        "qwen25-math"),
    _Route(("extract", "parse", "schema", "json"),         "granite4-350m"),
    _Route(("summarize", "tldr", "shorten"),               "minicpm4-05b"),
    _Route(("json", "tool", "function-call"),              "gemma3-270m-fn"),
    _Route(("multi-turn", "fallback", "retry"),            "danube3-500m"),
    _Route(("reason", "plan", "analyze", "chain"),         "llama32-1b-cap"),
)

_WORKSPACE_DEFAULTS: dict[str, str] = {
    "data": "granite4-350m", "design": "gemma3-270m", "core": "smollm2-360m",
}
_TASK_TYPE_DEFAULTS: dict[str, str] = {
    "extraction": "granite4-350m", "frontend": "gemma3-270m",
    "structuring": "smollm2-360m", "touch_fix": "qwen25-coder",
}


def pick_local_model(workspace: str, task_type: str, prompt: str) -> ModelSpec:
    """Scored routing. Deterministic. Never raises."""
    low = (prompt or "").lower().strip()
    best: _Route | None = None
    best_score = 0
    for route in _TASK_ROUTES:
        score = sum(1 for k in route.keywords if k in low)
        if score == 0:
            continue
        if (best is None or score > best_score
                or (score == best_score and route.model_id < best.model_id)):
            best = route
            best_score = score
    if best is not None:
        spec = ROSTER_BY_ID.get(best.model_id)
        if spec is not None:
            return spec
    if workspace in _WORKSPACE_DEFAULTS:
        return ROSTER_BY_ID[_WORKSPACE_DEFAULTS[workspace]]
    if task_type in _TASK_TYPE_DEFAULTS:
        return ROSTER_BY_ID[_TASK_TYPE_DEFAULTS[task_type]]
    return ROSTER_BY_ID["smollm2-360m"]


__all__ = [
    "Tier", "ModelSpec", "MODEL_ROSTER", "ROSTER_BY_ID",
    "MAX_RESIDENT_MB", "pick_local_model",
]