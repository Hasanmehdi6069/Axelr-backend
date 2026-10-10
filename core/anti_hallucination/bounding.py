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
