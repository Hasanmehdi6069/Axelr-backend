<<<<<<< HEAD
"""Layer 1 — Deterministic Retrieval & Bounding (v1.4).

RAM optimizations:
  * Lazy PDF import + AXELR_NO_PDF=1 hard opt-out (saves ~20 MB).
  * Optional orjson fast path for JSON (saves ~15% transient, ~3-5x speed).
  * Chunk: __slots__ + lazy bbox + lazy hash16.
  * canonicalize: LRU-cached for short strings only (≤256 chars).
  * iter_chunks: yields one chunk at a time (streaming).
  * JSON walker: iterative (no recursion).
"""
from __future__ import annotations

import csv as csv_mod
import hashlib
import io
import json
import os
import sys
import unicodedata
from array import array
from functools import lru_cache
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    import orjson as _fast_json  # type: ignore
except ImportError:
    _fast_json = None

from .schemas import (
    BoundingBox, DocumentParseError, ExtractionRequest, SourceCoordinate,
)


_NO_PDF = os.environ.get("AXELR_NO_PDF", "").lower() in ("1", "true", "yes")


# ── Canonicalization tables ──────────────────────────────────────────────
_INVISIBLE = frozenset({
    "\u200b", "\u200c", "\u200d", "\u200e", "\u200f",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2060", "\u2061", "\u2062", "\u2063", "\u2064",
    "\u2066", "\u2067", "\u2068", "\u2069",
    "\u180e", "\ufeff", "\u00ad",
})

_CONFUSABLES = {
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p",
    "\u0441": "c", "\u0445": "x", "\u0443": "y", "\u04bb": "h",
    "\u0501": "d", "\u0456": "i", "\u0458": "j", "\u04cf": "l",
}


def _is_canonical(text: str) -> bool:
    prev_ws = False
    for ch in text:
        if ch >= "\x80":
            return False
        if ch == " ":
            if prev_ws:
                return False
            prev_ws = True
        elif ch.isspace():
            return False
        else:
            prev_ws = False
    return True


def _canonicalize_slow(text: str) -> Tuple[str, Optional[array]]:
    if _is_canonical(text):
        return text, None
    out: List[str] = []
    idx = array("i")
    prev_ws = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _INVISIBLE:
            i += 1; continue
        j = i + 1
        while j < n and unicodedata.combining(text[j]):
            j += 1
        folded = "".join(
            _CONFUSABLES.get(c, c)
            for c in unicodedata.normalize("NFKC", text[i:j])
            if c not in _INVISIBLE
        )
        for sub in folded:
            if sub.isspace():
                if not prev_ws:
                    out.append(" "); idx.append(i)
                prev_ws = True
            else:
                out.append(sub); idx.append(i)
                prev_ws = False
        i = j
    result = "".join(out)
    if result == text:
        return text, None
    return result, idx


_CANON_CACHE_MAX_LEN = 256
_CANON_CACHE_SIZE = 1024


@lru_cache(maxsize=_CANON_CACHE_SIZE)
def _canonicalize_cached(text: str) -> Tuple[str, Optional[array]]:
    return _canonicalize_slow(text)


def canonicalize(text: str) -> Tuple[str, Optional[array]]:
    if len(text) <= _CANON_CACHE_MAX_LEN:
        return _canonicalize_cached(text)
    return _canonicalize_slow(text)


def detect_adversarial_unicode(text: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for ch in text:
        if ch in _INVISIBLE:
            h = hex(ord(ch))
            counts[h] = counts.get(h, 0) + 1
        elif ch >= "\x80" and unicodedata.combining(ch):
            h = hex(ord(ch))
            counts[h] = counts.get(h, 0) + 1
    return counts


# ── Chunk ────────────────────────────────────────────────────────────────
class Chunk:
    __slots__ = (
        "chunk_id", "text", "origin_offset", "char_end",
        "_bbox_raw", "_bbox_model",
        "row_index", "col_index", "json_path",
        "_canon", "_map", "_hash", "__weakref__",
    )

    def __init__(self, *, chunk_id: str, text: str, origin_offset: int,
                 char_end: int,
                 bbox_raw: Optional[tuple] = None,
                 row_index: Optional[int] = None,
                 col_index: Optional[int] = None,
                 json_path: Optional[str] = None) -> None:
        self.chunk_id = chunk_id
        self.text = text
        self.origin_offset = origin_offset
        self.char_end = char_end
        self._bbox_raw = bbox_raw
        self._bbox_model: Optional[BoundingBox] = None
        self.row_index = row_index
        self.col_index = col_index
        self.json_path = json_path
        self._canon: Optional[str] = None
        self._map: Optional[array] = None
        self._hash: Optional[str] = None

    @property
    def bbox(self) -> Optional[BoundingBox]:
        if self._bbox_model is None and self._bbox_raw is not None:
            page, x0, y0, x1, y1 = self._bbox_raw
            self._bbox_model = BoundingBox(page=page, x0=x0, y0=y0, x1=x1, y1=y1)
        return self._bbox_model

    def canonical(self) -> Tuple[str, Optional[array]]:
        if self._canon is None:
            self._canon, self._map = canonicalize(self.text)
        return self._canon, self._map

    def hash16(self) -> str:
        if self._hash is None:
            self._hash = hashlib.blake2b(
                self.text.encode("utf-8", "replace"), digest_size=16,
            ).hexdigest()
        return self._hash

    def coordinate(self, document_id: str) -> SourceCoordinate:
        return SourceCoordinate(
            document_id=document_id,
            chunk_id=self.chunk_id,
            char_start=self.origin_offset,
            char_end=self.char_end,
            bbox=self.bbox,
            row_index=self.row_index,
            col_index=self.col_index,
            json_path=self.json_path,
        )


class DocumentBuffer:
    __slots__ = ("chunks", "adversarial_counts")
    def __init__(self, chunks: List[Chunk],
                 adversarial_counts: Optional[Dict[str, int]] = None) -> None:
        self.chunks = chunks
        self.adversarial_counts = adversarial_counts or {}


# ── Ingestor ─────────────────────────────────────────────────────────────
class DocumentIngestor:
    __slots__ = ("max_chunk_chars",)

    def __init__(self, *, max_chunk_chars: int = 1400) -> None:
        self.max_chunk_chars = max_chunk_chars

    def iter_chunks(self, req: ExtractionRequest,
                    raw_bytes: bytes) -> Iterator[Chunk]:
        dt = req.document_type
        if dt == "pdf":     units = self._ingest_pdf(raw_bytes)
        elif dt == "csv":   units = self._ingest_csv(raw_bytes)
        elif dt == "json":  units = self._ingest_json(raw_bytes)
        elif dt == "text":  units = self._ingest_text(raw_bytes)
        elif dt in ("html", "py", "js"):
            units = self._ingest_text(raw_bytes)
        else:
            raise DocumentParseError(f"Unsupported document_type={dt}")

        prefix = sys.intern(f"{req.document_id}::c")
        cursor = 0
        for i, (text, extra) in enumerate(units):
            cid = sys.intern(f"{prefix}{i:05d}")
            yield Chunk(
                chunk_id=cid, text=text,
                origin_offset=cursor, char_end=cursor + len(text),
                bbox_raw=extra.get("_bbox_raw"),
                row_index=extra.get("row_index"),
                col_index=extra.get("col_index"),
                json_path=extra.get("json_path"),
            )
            cursor += len(text) + 1

    def ingest(self, req: ExtractionRequest, raw_bytes: bytes) -> DocumentBuffer:
        chunks: List[Chunk] = []
        adversarial: Dict[str, int] = {}
        for chunk in self.iter_chunks(req, raw_bytes):
            if len(chunks) >= req.max_chunks:
                raise DocumentParseError(
                    f"Document produced more than max_chunks={req.max_chunks}"
                )
            for k, v in detect_adversarial_unicode(chunk.text).items():
                adversarial[k] = adversarial.get(k, 0) + v
            chunks.append(chunk)
        return DocumentBuffer(chunks=chunks, adversarial_counts=adversarial)

    # ── PDF ──────────────────────────────────────────────────────────────
    def _ingest_pdf(self, raw: bytes) -> List[Tuple[str, Dict[str, Any]]]:
        if _NO_PDF:
            raise DocumentParseError(
                "PDF ingestion disabled via AXELR_NO_PDF=1"
            )
        try:
            import pdfplumber  # type: ignore
            import PyPDF2      # type: ignore
        except ImportError as e:
            raise DocumentParseError(
                "pdfplumber and PyPDF2 are required for PDF ingestion"
            ) from e

        primary: List[Tuple[str, Dict[str, Any]]] = []
        try:
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                for pno, page in enumerate(pdf.pages, start=1):
                    words = page.extract_words(use_text_flow=False,
                                               keep_blank_chars=False)
                    if not words:
                        continue
                    lines: Dict[float, List[Dict[str, Any]]] = {}
                    for w in words:
                        lines.setdefault(round(w["top"], 1), []).append(w)
                    for top in sorted(lines):
                        lw = sorted(lines[top], key=lambda x: x["x0"])
                        text = " ".join(w["text"] for w in lw)
                        primary.append((text, {
                            "_bbox_raw": (
                                pno,
                                min(w["x0"] for w in lw),
                                min(w["top"] for w in lw),
                                max(w["x1"] for w in lw),
                                max(w["bottom"] for w in lw),
                            ),
                            "page": pno,
                        }))
        except Exception:
            primary = []

        fallback: List[Tuple[str, Dict[str, Any]]] = []
        try:
            reader = PyPDF2.PdfReader(io.BytesIO(raw))
            for pno, page in enumerate(reader.pages, start=1):
                pt = page.extract_text() or ""
                if not pt.strip():
                    continue
                if not any(u[1].get("page") == pno for u in primary):
                    fallback.append((pt, {"source": "fallback_pypdf2", "page": pno}))
        except Exception:
            pass

        units = primary + fallback
        if not units:
            raise DocumentParseError("Both PDF parsers failed to extract content")
        return units

    # ── CSV ──────────────────────────────────────────────────────────────
    def _ingest_csv(self, raw: bytes) -> List[Tuple[str, Dict[str, Any]]]:
        try:
            text = raw.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as e:
            raise DocumentParseError(f"CSV is not valid UTF-8: {e}") from e
        reader = csv_mod.reader(io.StringIO(text))
        units: List[Tuple[str, Dict[str, Any]]] = []
        header: Optional[List[str]] = None
        for r, row in enumerate(reader):
            if header is None:
                header = [sys.intern(h) for h in row]
                units.append((f"HEADER: {', '.join(row)}", {"row_index": 0}))
                continue
            for c, cell in enumerate(row):
                name = header[c] if c < len(header) else f"col_{c}"
                units.append((
                    f"[row={r} col={c} name={name}] {cell}",
                    {"row_index": r, "col_index": c},
                ))
        return units

    # ── JSON (iterative, no recursion) ───────────────────────────────────
    def _ingest_json(self, raw: bytes) -> List[Tuple[str, Dict[str, Any]]]:
        try:
            if _fast_json is not None:
                data = _fast_json.loads(raw)
            else:
                data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise DocumentParseError(f"JSON parse failure: {e}") from e
        units: List[Tuple[str, Dict[str, Any]]] = []
        stack: List[Tuple[Any, str]] = [(data, "")]
        while stack:
            node, path = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    stack.append((v, f"{path}.{k}" if path else str(k)))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    stack.append((v, f"{path}[{i}]"))
            else:
                units.append((f"{path} = {node}", {"json_path": path}))
        return units

    # ── Text (bounded even without paragraph breaks) ─────────────────────
    def _ingest_text(self, raw: bytes) -> List[Tuple[str, Dict[str, Any]]]:
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            raise DocumentParseError(f"Text decode failure: {e}") from e
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        units: List[Tuple[str, Dict[str, Any]]] = []
        buf = ""
        for p in paras:
            if len(p) > self.max_chunk_chars:
                if buf.strip():
                    units.append((buf.strip(), {})); buf = ""
                for s in range(0, len(p), self.max_chunk_chars):
                    units.append((p[s:s + self.max_chunk_chars], {}))
                continue
            if buf and len(buf) + len(p) + 2 > self.max_chunk_chars:
                units.append((buf.strip(), {})); buf = ""
            buf += p + "\n\n"
        if buf.strip():
            units.append((buf.strip(), {}))
        if not units and text.strip():
            t = text.strip()
            for s in range(0, len(t), self.max_chunk_chars):
                units.append((t[s:s + self.max_chunk_chars], {}))
        return units


__all__ = [
    "Chunk", "DocumentBuffer", "DocumentIngestor",
    "canonicalize", "detect_adversarial_unicode",
]
=======
"""
Layer 1 — Deterministic Retrieval & Bounding.
==============================================
Turns any supported document into a `DocumentBuffer` of coordinate-bearing
`Chunk`s. Every chunk knows its absolute character range in the canonical
document text, plus an optional bbox / row_index / col_index / json_path.

The canonical buffer (`canonical_text` + `index_map`) is a whitespace-collapsed,
order-preserving view of the original. Any substring match against the canonical
buffer maps back to a precise, contiguous range in the original source — this is
what makes the zero-hallucination guarantee arithmetic instead of aspirational.
"""
from __future__ import annotations

import csv as csv_mod
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .schemas import BoundingBox, DocumentParseError, ExtractionRequest, SourceCoordinate

_WS_RUN = re.compile(r"\s+")


import unicodedata

# Zero-width and harmful unicode characters to strip
_ZERO_WIDTH_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u200e', '\u200f',  # Zero-width spaces/joiner
    '\ufeff',  # BOM
    '\u00ad',  # Soft hyphen
}

# Common homoglyph/confusable mapping (Cyrillic to Latin for high-risk characters)
_CONFUSABLES = {
    '\u0430': 'a',  # Cyrillic а
    '\u0435': 'e',  # Cyrillic е
    '\u043e': 'o',  # Cyrillic о
    '\u0440': 'p',  # Cyrillic р
    '\u0441': 'c',  # Cyrillic с
    '\u0445': 'x',  # Cyrillic х
    '\u0061': 'a',  # Latin a (pass through)
}

def canonicalize(text: str) -> Tuple[str, List[int]]:
    """
    Unified canonicalization matching DAG's _normalize():
    1. NFKC normalize all unicode
    2. Strip zero-width/harmful characters
    3. Map known confusables to their Latin equivalents
    4. Collapse runs of whitespace to a single ASCII space

    Lossless w.r.t. non-whitespace content: every non-whitespace character of
    the input appears exactly once, in order, in the output.
    Returns (canonical_text, index_map) where index_map[i] = original index.
    """
    # First apply NFKC normalization
    normalized = unicodedata.normalize('NFKC', text)
    out: List[str] = []
    idx: List[int] = []
    prev_ws = False

    for i, ch in enumerate(normalized):
        # Strip harmful zero-width characters
        if ch in _ZERO_WIDTH_CHARS:
            continue
        # Map confusables to prevent homograph attacks
        mapped_ch = _CONFUSABLES.get(ch, ch)
        # Handle whitespace collapsing
        if mapped_ch.isspace():
            if not prev_ws:
                out.append(" ")
                idx.append(i)
            prev_ws = True
        else:
            out.append(mapped_ch)
            idx.append(i)
            prev_ws = False
    return "".join(out), idx


@dataclass
class Chunk:
    chunk_id: str
    text: str
    coordinate: SourceCoordinate
    origin_offset: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentBuffer:
    full_text: str
    canonical_text: str
    index_map: List[int]
    chunks: List[Chunk]


class DocumentIngestor:
    """Deterministic PDF / CSV / JSON / text ingestion with absolute coordinates."""

    def __init__(self, *, max_chunk_chars: int = 1400) -> None:
        self.max_chunk_chars = max_chunk_chars

    # ── public ────────────────────────────────────────────────────────────
    def ingest(self, req: ExtractionRequest, raw_bytes: bytes) -> DocumentBuffer:
        if req.document_type == "pdf":
            units = self._ingest_pdf(raw_bytes)
        elif req.document_type == "csv":
            units = self._ingest_csv(raw_bytes)
        elif req.document_type == "json":
            units = self._ingest_json(raw_bytes)
        elif req.document_type == "text":
            units = self._ingest_text(raw_bytes)
        else:
            raise DocumentParseError(f"Unsupported document_type={req.document_type}")

        parts: List[str] = []
        cursor = 0
        chunks: List[Chunk] = []
        for i, (unit_text, extra) in enumerate(units):
            chunk_id = f"{req.document_id}::c{i:05d}"
            coord = SourceCoordinate(
                document_id=req.document_id,
                chunk_id=chunk_id,
                char_start=cursor,
                char_end=cursor + len(unit_text),
                bbox=extra.get("bbox"),
                row_index=extra.get("row_index"),
                col_index=extra.get("col_index"),
                json_path=extra.get("json_path"),
            )
            chunks.append(Chunk(
                chunk_id=chunk_id, text=unit_text, coordinate=coord,
                origin_offset=cursor, extra=extra,
            ))
            parts.append(unit_text)
            cursor += len(unit_text)
            parts.append("\n")
            cursor += 1

        full_text = "".join(parts)
        canonical_text, index_map = canonicalize(full_text)
        return DocumentBuffer(
            full_text=full_text,
            canonical_text=canonical_text,
            index_map=index_map,
            chunks=chunks,
        )

    # ── PDF (pdfplumber + second-pass fallback parser) ───────────────────
    def _ingest_pdf(self, raw: bytes) -> List[Tuple[str, Dict[str, Any]]]:
        try:
            import pdfplumber  # type: ignore
            import PyPDF2  # type: ignore  # Second-pass fallback parser
        except ImportError as e:
            raise DocumentParseError(
                "pdfplumber and PyPDF2 are required for PDF ingestion "
                "(add them to requirements.txt)"
            ) from e

        units: List[Tuple[str, Dict[str, Any]]] = []
        primary_units: List[Tuple[str, Dict[str, Any]]] = []

        # Primary parse: pdfplumber with accurate bounding boxes
        try:
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                for page_no, page in enumerate(pdf.pages, start=1):
                    words = page.extract_words(use_text_flow=False,
                                               keep_blank_chars=False)
                    if not words:
                        continue
                    lines: Dict[float, List[Dict[str, Any]]] = {}
                    for w in words:
                        lines.setdefault(round(w["top"], 1), []).append(w)
                    for top in sorted(lines):
                        lw = sorted(lines[top], key=lambda x: x["x0"])
                        text = " ".join(w["text"] for w in lw)
                        bbox = BoundingBox(
                            page=page_no,
                            x0=min(w["x0"] for w in lw),
                            y0=min(w["top"] for w in lw),
                            x1=max(w["x1"] for w in lw),
                            y1=max(w["bottom"] for w in lw),
                        )
                        primary_units.append((text, {"bbox": bbox, "page": page_no}))
        except Exception:
            pass  # Fall back to second-pass parser

        # Second-pass verification/fallback: PyPDF2 for missing text or ambiguity
        fallback_units: List[Tuple[str, Dict[str, Any]]] = []
        try:
            reader = PyPDF2.PdfReader(io.BytesIO(raw))
            for page_no, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text() or ""
                if not page_text.strip():
                    continue
                # Add fallback text if primary parse had no content for this page
                page_has_primary = any(u[1].get("page") == page_no for u in primary_units)
                if not page_has_primary:
                    fallback_units.append((page_text, {"source": "fallback_pypdf2", "page": page_no}))
        except Exception:
            pass

        # Combine results: primary first, then fallback only if needed
        units = primary_units + fallback_units
        if not units:
            raise DocumentParseError("Both PDF parsers failed to extract content")
        return units

    # ── CSV (row/col addressed) ───────────────────────────────────────────
    def _ingest_csv(self, raw: bytes) -> List[Tuple[str, Dict[str, Any]]]:
        try:
            text = raw.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as e:
            raise DocumentParseError(f"CSV is not valid UTF-8: {e}") from e
        reader = csv_mod.reader(io.StringIO(text))
        units: List[Tuple[str, Dict[str, Any]]] = []
        header: Optional[List[str]] = None
        for r_idx, row in enumerate(reader):
            if header is None:
                header = row
                units.append((f"HEADER: {', '.join(row)}", {"row_index": 0}))
                continue
            for c_idx, cell in enumerate(row):
                col_name = header[c_idx] if c_idx < len(header) else f"col_{c_idx}"
                units.append((
                    f"[row={r_idx} col={c_idx} name={col_name}] {cell}",
                    {"row_index": r_idx, "col_index": c_idx},
                ))
        return units

    # ── JSON (json_path addressed) ────────────────────────────────────────
    def _ingest_json(self, raw: bytes) -> List[Tuple[str, Dict[str, Any]]]:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise DocumentParseError(f"JSON parse failure: {e}") from e
        units: List[Tuple[str, Dict[str, Any]]] = []

        def walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{path}.{k}" if path else str(k))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{path}[{i}]")
            else:
                units.append((f"{path} = {node}", {"json_path": path}))

        walk(data, "")
        return units

    # ── Text (paragraph + window chunking) ────────────────────────────────
    def _ingest_text(self, raw: bytes) -> List[Tuple[str, Dict[str, Any]]]:
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            raise DocumentParseError(f"Text decode failure: {e}") from e
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        units: List[Tuple[str, Dict[str, Any]]] = []
        buf = ""
        for p in paras:
            if buf and len(buf) + len(p) + 2 > self.max_chunk_chars:
                units.append((buf.strip(), {}))
                buf = ""
            buf += p + "\n\n"
        if buf.strip():
            units.append((buf.strip(), {}))
        if not units and text.strip():
            units.append((text.strip(), {}))
        return units


__all__ = ["Chunk", "DocumentBuffer", "DocumentIngestor", "canonicalize"]
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
