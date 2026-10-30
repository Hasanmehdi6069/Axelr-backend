"""LLM-free deterministic extraction (v1.4).

Content-addressed claim cache (CACC) with bounded LRU. Bounded `@lru_cache`
on label normalization. Unmapped labels raise; no silent coercion.
"""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from functools import lru_cache
from typing import List, Optional, Tuple

from .bounding import Chunk
from .schemas import ExtractedClaim


@lru_cache(maxsize=512)
def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


_LABEL_RE = re.compile(
    r"""(?P<label>[A-Za-z][A-Za-z0-9 _\-/]{1,63}?)\s*[:=]\s*
        (?P<value>
            (?!\s*[A-Za-z][A-Za-z0-9 _\-/]{1,63}\s*[:=])
            [^\n\r]{1,256}
        )""",
    re.VERBOSE,
)


def _blake2b_16(s: str) -> bytes:
    return hashlib.blake2b(s.encode("utf-8", "replace"), digest_size=16).digest()


class DeterministicExtractor:
    __slots__ = ("_cache", "_cache_size")

    def __init__(self, *, cache_size: int = 1024) -> None:
        self._cache: "OrderedDict[tuple, list[ExtractedClaim]]" = OrderedDict()
        self._cache_size = cache_size

    def extract(self, *, query: str, target_fields: List[str],
                chunks: List[Chunk], **_) -> List[ExtractedClaim]:
        fields_key = tuple(sorted(target_fields))
        wanted = frozenset(_norm_label(f) for f in target_fields)
        out: List[ExtractedClaim] = []
        for chunk in chunks:
            key = (fields_key, _blake2b_16(chunk.text))
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                out.extend(hit)
                continue
            claims = self._extract_one(chunk, wanted, target_fields)
            self._cache[key] = claims
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            out.extend(claims)
        return out

    @staticmethod
    def _extract_one(chunk: Chunk, wanted: frozenset,
                     target_fields: List[str]) -> List[ExtractedClaim]:
        claims: List[ExtractedClaim] = []
        seen: set[Tuple[str, str]] = set()
        for m in _LABEL_RE.finditer(chunk.text):
            label = _norm_label(m.group("label"))
            if label not in wanted:
                continue
            value = m.group("value").strip()
            snippet = m.group(0)
            key = (label, value)
            if key in seen:
                continue
            seen.add(key)
            claims.append(ExtractedClaim(
                field_name=DeterministicExtractor._canonical_field(label, target_fields),
                value=value,
                raw_snippet=snippet,
                chunk_id=chunk.chunk_id,
                trivial=True,
            ))
        return claims

    @staticmethod
    def _canonical_field(norm_label: str, target_fields: List[str]) -> str:
        for f in target_fields:
            if _norm_label(f) == norm_label:
                return f
        raise AssertionError(f"unmapped label {norm_label!r} — refusing coercion")


__all__ = ["DeterministicExtractor"]