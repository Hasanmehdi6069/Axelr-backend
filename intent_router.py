# intent_router.py
"""
AXELR Intent Router
===================
Two-tier workspace classifier:

  Tier 1 (always on): keyword + file-extension rules — sub-millisecond.
  Tier 2 (lazy):      ONNX Runtime + Xenova/mobilebert-uncased-mnli (~21 MB).

Returns ``{workspace, confidence, method}``.

The ONNX session is created only on first ambiguous request; if onnxruntime
or the model directory is unavailable, the router silently stays on Tier 1.

RAM footprint: < 5 MB idle, < 45 MB after ONNX model load.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["IntentRouter", "IntentResult", "get_router"]


# ---------------------------------------------------------------------------
# Keyword rules (compiled once)
# ---------------------------------------------------------------------------

_DESIGN_KEYWORDS = re.compile(
    r"""(?ix)
    \b(
        design|ui|ux|mockup|wireframe|frontend|front-end|front\ end|
        html|css|scss|tailwind|bootstrap|react|vue|svelte|angular|
        component|navbar|sidebar|landing\ page|dashboard|layout|
        button|modal|dropdown|responsive|animation|svg|icon|
        prototype|figma|sketch|theme|dark\ mode
    )\b
    """
)

_DATA_KEYWORDS = re.compile(
    r"""(?ix)
    \b(
        extract|analyse|analyze|analysis|dataset|data\ set|csv|tsv|
        excel|xlsx|spreadsheet|sheet|table|column|row|pivot|
        aggregate|summarise|summarize|report|metric|kpi|chart|
        graph|plot|invoice|receipt|tabular|sql|query|database|
        etl|clean|normalise|normalize|pandas|dataframe
    )\b
    """
)

_CODE_KEYWORDS = re.compile(
    r"""(?ix)
    \b(
        function|class|refactor|debug|bug|error|stack\ trace|
        exception|compile|lint|unit\ test|pytest|jest|api\ endpoint|
        regex|algorithm|async|await|promise|closure|recursion
    )\b
    """
)


# ---------------------------------------------------------------------------
# File-extension buckets
# ---------------------------------------------------------------------------

_DATA_EXTS = {
    ".csv", ".tsv", ".xls", ".xlsx", ".xlsm", ".ods",
    ".parquet", ".feather", ".sql", ".db", ".sqlite",
    ".pdf", ".doc", ".docx",
}

_DESIGN_EXTS = {
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".fig", ".sketch",
}

_CODE_EXTS = {
    ".py", ".pyi", ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".kt", ".swift", ".scala",
    ".sh", ".bash", ".zsh", ".ps1",
}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class IntentResult:
    """Outcome of an intent classification."""

    workspace: str            # "design" | "data" | "core"
    confidence: float         # 0.0 – 1.0
    method: str               # "extension" | "rules" | "onnx" | "fallback"

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""
        return {
            "workspace": self.workspace,
            "confidence": round(self.confidence, 4),
            "method": self.method,
        }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class IntentRouter:
    """
    Two-tier intent router.

    Parameters
    ----------
    model_dir : str | None
        Directory containing the ONNX model (``model.onnx``) and tokenizer
        files (``tokenizer.json``). If ``None``, reads ``INTENT_MODEL_DIR``
        from the environment.
    confidence_threshold : float
        Minimum rule-based confidence to short-circuit ONNX. Defaults to 0.75.
    """

    __slots__ = (
        "_model_dir",
        "_confidence_threshold",
        "_session",
        "_tokenizer",
        "_session_lock",
        "_labels",
    )

    def __init__(
        self,
        model_dir: Optional[str] = None,
        *,
        confidence_threshold: float = 0.75,
    ) -> None:
        """Initialise the router; ONNX loads lazily on first ambiguous request."""
        _raw = (model_dir or os.getenv("INTENT_MODEL_DIR") or "").strip()
        # ── FIX: use walrus for clean optional-path resolution ──────────
        self._model_dir: Optional[Path] = (
            Path(raw)
            if (raw := (model_dir or os.getenv("INTENT_MODEL_DIR")))
            else None
        )
        self._confidence_threshold = float(confidence_threshold)
        self._session = None
        self._tokenizer = None
        self._session_lock = asyncio.Lock()
        # MobileBERT-MNLI was trained on these labels.
        self._labels: List[str] = ["data", "design", "core"]

    # -- public API ---------------------------------------------------------

    async def classify(
        self,
        prompt: str,
        files: Optional[Sequence[Dict[str, str]]] = None,
    ) -> IntentResult:
        """
        Classify the user request.

        ``files`` is a sequence of dicts each containing at least a
        ``filename`` key (mimetype optional). Never raises.
        """
        try:
            return await self._classify_inner(prompt, files)
        except Exception:
            return IntentResult("core", 0.0, "fallback")

    async def _classify_inner(
        self,
        prompt: str,
        files: Optional[Sequence[Dict[str, str]]],
    ) -> IntentResult:
        prompt = (prompt or "").strip()
        files = list(files or [])

        # 1. File-extension signal
        ext_ws, ext_conf = self._classify_by_files(files)

        # 2. Keyword signal
        kw_ws, kw_conf = self._classify_by_keywords(prompt)

        # 3. Combine — file hints win when strong
        if ext_conf >= 0.9:
            return IntentResult(ext_ws, ext_conf, "extension")

        if kw_conf >= self._confidence_threshold:
            if ext_ws == kw_ws and ext_conf > 0.4:
                return IntentResult(kw_ws, min(1.0, kw_conf + 0.15), "rules")
            return IntentResult(kw_ws, kw_conf, "rules")

        # 4. Ambiguous — try ONNX if configured
        if self._session is not None or self._model_dir is not None:
            onnx_result = await self._classify_onnx(prompt)
            if onnx_result is not None:
                return onnx_result

        # 5. Fallback — use best available weak signal
        if ext_ws != "core":
            return IntentResult(ext_ws, max(0.5, ext_conf), "fallback")
        if kw_ws != "core":
            return IntentResult(kw_ws, max(0.4, kw_conf), "fallback")
        return IntentResult("core", 0.5, "fallback")

    def warm_up(self) -> None:
        """Eagerly load the ONNX model at startup (optional). Never raises."""
        if self._model_dir is None:
            return
        if self._session is None:
            try:
                self._load_onnx()
            except Exception:
                self._session = None

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _classify_by_files(
        files: Sequence[Dict[str, str]],
    ) -> Tuple[str, float]:
        """Return (workspace, confidence) from file extensions/mimetypes."""
        if not files:
            return "core", 0.0

        data_score = 0
        design_score = 0
        code_score = 0
        image_score = 0

        for f in files:
            name = (f.get("filename") or "").lower()
            ext = Path(name).suffix.lower()
            mimetype = (f.get("mimetype") or "").lower()

            if ext in _DATA_EXTS:
                data_score += 2
            elif ext in _IMAGE_EXTS or mimetype.startswith("image/"):
                image_score += 1
                design_score += 1
            elif ext in _DESIGN_EXTS:
                design_score += 1
            elif ext in _CODE_EXTS:
                code_score += 1

        best = max(data_score, design_score, code_score, image_score)
        if best == 0:
            return "core", 0.0

        if best == data_score and data_score >= 2:
            return "data", 0.9
        if best == design_score and design_score >= 2:
            return "design", 0.85
        if best == code_score and code_score >= 2:
            return "design", 0.8

        # Weak signal
        if data_score > design_score and data_score > code_score:
            return "data", 0.55
        if design_score > data_score and design_score > code_score:
            return "design", 0.55
        return "core", 0.3

    @staticmethod
    def _classify_by_keywords(prompt: str) -> Tuple[str, float]:
        """Return (workspace, confidence) from keyword rules."""
        if not prompt:
            return "core", 0.0

        design_hits = len(_DESIGN_KEYWORDS.findall(prompt))
        data_hits = len(_DATA_KEYWORDS.findall(prompt))
        code_hits = len(_CODE_KEYWORDS.findall(prompt))

        total = design_hits + data_hits + code_hits
        if total == 0:
            return "core", 0.0

        weights = {
            "design": design_hits * 1.0,
            "data": data_hits * 1.0,
            "core": code_hits * 0.6,
        }
        best_ws = max(weights, key=weights.get)  # type: ignore[arg-type]
        best_score = weights[best_ws]
        confidence = min(1.0, best_score / max(1.0, total) * 1.4)
        return best_ws, confidence

    async def _classify_onnx(self, prompt: str) -> Optional[IntentResult]:
        """Run the ONNX classifier. Returns ``None`` on any failure."""
        if self._session is None and self._model_dir is not None:
            async with self._session_lock:
                if self._session is None:
                    try:
                        await asyncio.to_thread(self._load_onnx)
                    except Exception:
                        self._session = None
                        return None

        if self._session is None or self._tokenizer is None:
            return None

        try:
            encoded = self._tokenizer.encode(prompt)
            input_ids = list(encoded.ids[:256])
            attention_mask = list(encoded.attention_mask[:256])

            pad_id = 0
            max_len = 256
            if len(input_ids) < max_len:
                pad = max_len - len(input_ids)
                input_ids = input_ids + [pad_id] * pad
                attention_mask = attention_mask + [0] * pad
            else:
                input_ids = input_ids[:max_len]
                attention_mask = attention_mask[:max_len]

            # Lazy numpy import
            import numpy as np

            inputs = {
                "input_ids": np.asarray([input_ids], dtype=np.int64),
                "attention_mask": np.asarray([attention_mask], dtype=np.int64),
            }
            try:
                input_names = {i.name for i in self._session.get_inputs()}
                if "token_type_ids" in input_names:
                    inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])
            except Exception:
                pass

            outputs = await asyncio.to_thread(self._session.run, None, inputs)
            logits = outputs[0][0]
            exp = np.exp(logits - logits.max())
            probs = exp / exp.sum()
            idx = int(probs.argmax())
            label = self._labels[idx] if idx < len(self._labels) else "core"
            confidence = float(probs[idx])
            if confidence < 0.5:
                return None
            return IntentResult(label, confidence, "onnx")
        except Exception:
            return None

    def _load_onnx(self) -> None:
        """Synchronous ONNX load — invoked via ``asyncio.to_thread``."""
        if self._model_dir is None:
            raise RuntimeError("no model dir configured")

        model_path = self._model_dir / "model.onnx"
        if not model_path.is_file():
            raise FileNotFoundError(f"missing {model_path}")

        # Lazy heavy imports
        import onnxruntime as ort  # type: ignore
        from tokenizers import Tokenizer  # type: ignore

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = 1
        sess_options.inter_op_num_threads = 1
        sess_options.graph_optimization_level = (
            ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        )
        session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )

        tokenizer_path = self._model_dir / "tokenizer.json"
        if not tokenizer_path.is_file():
            raise FileNotFoundError(f"missing {tokenizer_path}")
        tokenizer = Tokenizer.from_file(str(tokenizer_path))

        self._session = session
        self._tokenizer = tokenizer


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_router: Optional[IntentRouter] = None


def get_router() -> IntentRouter:
    """Return a lazily-instantiated singleton router."""
    global _default_router
    if _default_router is None:
        _default_router = IntentRouter()
    return _default_router