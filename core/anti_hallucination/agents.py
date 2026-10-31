<<<<<<< HEAD
"""Layer 2 — Adversarial Multi-Agent Adjudication (v1.5).

Adds: circuit breaker integration, matched_text minimization,
deterministic finalization order. Preserves D1, D3, D6 fixes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from typing import Dict, List, Optional

try:
    import orjson as _fast_json  # type: ignore
except ImportError:
    _fast_json = None

from pydantic import ValidationError

from .bounding import Chunk, DocumentBuffer, canonicalize
from .deterministic import DeterministicExtractor
from ._verify import (
    LineageIndex, contradiction, negation_window,
    REASON_OK, REASON_CHUNK_NOT_FOUND, REASON_EMPTY_SNIPPET,
    REASON_SHORT_SNIPPET, REASON_NOT_SUBSTRING, REASON_VALUE_NOT_DERIVABLE,
    REASON_STRICT_THRESHOLD, REASON_WEAK_VALUE, REASON_QUERY_ECHO,
    REASON_QUERY_SIMILAR, REASON_NEGATED, REASON_DETERMINISTIC,
    REASON_VERBATIM,
)
from ._runtime import CircuitBreaker, intern_str
from .schemas import (
    ExtractedClaim, NoProviderAvailableError, SchemaViolationError,
    SourceCoordinate, UnverifiableDataError, VerificationResult,
    VerifiedClaim, VerifiedPayload, compute_audit_hash,
)

log = logging.getLogger("axelr.ahe.agents")


EXTRACTOR_SYSTEM_PROMPT = """\
You are Axelr's EXTRACTION AGENT. You are an UNTRUSTED PROPOSER inside a
zero-hallucination pipeline. Your output is NOT shown to the user; it is
cross-examined by a deterministic string-matcher and rejected if ANY claim
fails to match the source character-for-character.

ABSOLUTE RULES:
1. Return ONLY valid JSON. No prose. No markdown fences. No commentary.
2. The JSON must be: {"claims": [ ... ]}.
3. Each claim MUST have EXACTLY these keys:
     "field_name"  : one of the requested target fields (verbatim)
     "value"       : the extracted value, as a string
     "raw_snippet" : the EXACT, VERBATIM, CONTIGUOUS substring of the source
                     chunk from which "value" is derived.
     "chunk_id"    : the chunk_id containing that snippet.
4. If a requested field is NOT verbatim-present in any chunk, OMIT it entirely.
5. Do NOT include any key beyond the four above on a claim.
6. Numeric formatting must match the source EXACTLY ("1,234.50", not "1234.5").
"""


class ExtractorAgent:
    def __init__(self, *, timeout_s: float = 60.0,
                 max_output_tokens: int = 4096,
                 max_self_correction_retries: int = 2,
                 allow_llm: bool = True,
                 circuit_breaker: Optional[CircuitBreaker] = None) -> None:
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens
        self.max_self_correction_retries = max_self_correction_retries
        self.allow_llm = allow_llm
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self._det = DeterministicExtractor()

    async def extract(self, *, query: str, target_fields: List[str],
                      chunks: List[Chunk], workspace: str, tier: str,
                      correction_hint: Optional[str] = None,
                      ) -> List[ExtractedClaim]:
        det = self._det.extract(
            query=query, target_fields=target_fields, chunks=chunks,
        )
        covered = {c.field_name for c in det}
        missing = [f for f in target_fields if f not in covered]
        if not missing:
            return det
        if not self.allow_llm:
            return det
        if self.circuit_breaker.is_open():
            log.debug("circuit_open_skip_llm")
            return det
        try:
            llm = await self._extract_via_llm(
                query=query, target_fields=missing, chunks=chunks,
                workspace=workspace, tier=tier,
                correction_hint=correction_hint,
            )
        except (SchemaViolationError, NoProviderAvailableError) as e:
            if det:
                log.warning("llm_unavailable err=%s kept=%d",
                            type(e).__name__, len(det))
                return det
            raise
        return det + llm

    async def _extract_via_llm(self, *, query, target_fields, chunks,
                               workspace, tier, correction_hint=None):
        user_prompt = self._build_user_prompt(
            query, target_fields, chunks, correction_hint,
        )
        raw = await self._call_llm(
            EXTRACTOR_SYSTEM_PROMPT, user_prompt, workspace=workspace, tier=tier,
        )
        return self._parse(raw, target_fields)

    @staticmethod
    def _build_user_prompt(query, target_fields, chunks, correction_hint):
        payload = [
            {
                "chunk_id": c.chunk_id,
                "text": c.text,
                "coordinate": {
                    "row_index": c.row_index,
                    "col_index": c.col_index,
                    "json_path": c.json_path,
                    "page": c.bbox.page if c.bbox else None,
                },
            }
            for c in chunks
        ]
        hint = ""
        if correction_hint:
            hint = f"\n\nPREVIOUS FAILED. FIX:\n<<<{correction_hint}>>>\n"
        return (
            f"TARGET FIELDS:\n{json.dumps(target_fields)}\n\n"
            f"USER QUERY:\n{query}\n\n"
            f"SOURCE CHUNKS:\n{json.dumps(payload, ensure_ascii=False)}\n"
            f"{hint}\nReturn ONLY the JSON object {{\"claims\": [...]}}."
        )

    async def _call_llm(self, system: str, user: str,
                        *, workspace: str, tier: str) -> str:
        try:
            from api.providers import (  # type: ignore
                PROVIDER_FUNC_MAP, PROVIDER_KEY_CHECK, PROVIDER_MODELS,
                get_dynamically_ranked_providers, record_provider_result,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("providers_unavailable error=%s", e)
            return await self._local_fallback(system, user, workspace)

        full_prompt = f"{system}\n\n{user}"
        ranked = await get_dynamically_ranked_providers(workspace)
        for provider_name in ranked:
            if provider_name == "local":
                continue
            if not PROVIDER_KEY_CHECK.get(provider_name, False):
                continue
            func = PROVIDER_FUNC_MAP.get(provider_name)
            models = PROVIDER_MODELS.get(provider_name) or []
            if not func or not models:
                continue
            model = models[0]
            try:
                resp = await asyncio.wait_for(
                    func(full_prompt, self.max_output_tokens, 0.0, model),
                    timeout=self.timeout_s,
                )
                if resp and resp.strip():
                    self.circuit_breaker.record_success()
                    await record_provider_result(provider_name, 0.0, success=True)
                    return resp
            except Exception as e:  # noqa: BLE001
                log.debug("provider_failed provider=%s error=%s",
                          provider_name, e)
                self.circuit_breaker.record_failure()
                await record_provider_result(provider_name, 0.0, success=False)
                continue
        return await self._local_fallback(system, user, workspace)

    async def _local_fallback(self, system: str, user: str,
                              workspace: str) -> str:
        try:
            from core.twin_engine import execute_local_inference  # type: ignore
            text = await asyncio.wait_for(
                execute_local_inference(
                    workspace, "extraction", f"{system}\n\n{user}",
                ),
                timeout=self.timeout_s,
            )
            if text and not text.lstrip().startswith("[stub]"):
                return text
        except Exception as e:  # noqa: BLE001
            log.debug("local_fallback_failed error=%s", e)
        raise NoProviderAvailableError(
            "No provider (remote or local) produced a response for the extractor."
        )

    @staticmethod
    def _strip_fences(s: str) -> str:
        s = s.strip()
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z0-9]*\n", "", s)
            s = re.sub(r"\n```\s*$", "", s)
        return s

    def _parse(self, raw: str, target_fields: List[str]) -> List[ExtractedClaim]:
        cleaned = self._strip_fences(raw)
        try:
            if _fast_json is not None:
                obj = _fast_json.loads(cleaned.encode("utf-8"))
            else:
                obj = json.loads(cleaned)
        except Exception as e:  # noqa: BLE001
            raise SchemaViolationError(
                f"LLM did not return valid JSON: {e}",
                {"raw_preview": raw[:512]},
            )
        if not isinstance(obj, dict) or not isinstance(obj.get("claims"), list):
            raise SchemaViolationError(
                "LLM output must be {'claims': [...]}",
                {"raw_preview": raw[:512]},
            )
        out: List[ExtractedClaim] = []
        for i, c in enumerate(obj["claims"]):
            if not isinstance(c, dict):
                raise SchemaViolationError(f"claims[{i}] is not an object")
            try:
                claim = ExtractedClaim.model_validate(c)
            except ValidationError as e:
                raise SchemaViolationError(
                    f"claims[{i}] violated schema",
                    {"errors": e.errors(include_url=False)},
                ) from e
            if claim.field_name not in target_fields:
                log.warning("dropping un-requested field %r", claim.field_name)
                continue
            out.append(claim)
        return out


_STRICT_FIELD_TOKENS = frozenset({
    "amount", "total", "price", "cost", "fee", "balance",
    "id", "code", "number", "date", "time", "email", "phone",
})
_FIELD_TOK_RE = re.compile(r"[a-z0-9]+")


class SkepticAgent:
    MIN_SNIPPET_CANON_CHARS = 4
    MIN_WEAK_RATIO = 20
    STRICT_FIELD_RATIO = 0.90
    QUERY_ECHO_SIMILARITY = 0.90

    def cross_examine(self, claim: ExtractedClaim,
                      chunks_by_id: Dict[str, Chunk],
                      doc: DocumentBuffer,
                      query: str = "") -> VerificationResult:
        chunk = chunks_by_id.get(claim.chunk_id)
        if chunk is None:
            return VerificationResult(
                verified=False,
                reason=f"chunk_id '{claim.chunk_id}' not found",
                reason_code=REASON_CHUNK_NOT_FOUND,
            )

        if claim.trivial:
            start = chunk.text.find(claim.raw_snippet)
            if start >= 0:
                return VerificationResult(
                    verified=True,
                    reason="deterministic verbatim (trivial fast path)",
                    reason_code=REASON_DETERMINISTIC,
                    char_start=chunk.origin_offset + start,
                    char_end=chunk.origin_offset + start + len(claim.raw_snippet),
                    matched_text=None,
                )

        chunk_canon, chunk_map = chunk.canonical()
        snippet_canon, _ = canonicalize(claim.raw_snippet)
        if not snippet_canon:
            return VerificationResult(
                verified=False, reason="empty raw_snippet",
                reason_code=REASON_EMPTY_SNIPPET,
            )

        if len(snippet_canon) < self.MIN_SNIPPET_CANON_CHARS:
            if claim.raw_snippet not in chunk.text:
                return VerificationResult(
                    verified=False,
                    reason="short snippet: exact raw substring required",
                    reason_code=REASON_SHORT_SNIPPET,
                )
            local_start = chunk.text.find(claim.raw_snippet)
            local_end = local_start + len(claim.raw_snippet)
        else:
            pos = chunk_canon.find(snippet_canon)
            if pos < 0:
                return VerificationResult(
                    verified=False,
                    reason="raw_snippet is not a verbatim substring",
                    reason_code=REASON_NOT_SUBSTRING,
                )
            if chunk_map is None:
                local_start = pos
                local_end = pos + len(snippet_canon)
            else:
                local_start = chunk_map[pos]
                local_end = chunk_map[pos + len(snippet_canon) - 1] + 1

        matched = chunk.text[local_start:local_end]
        abs_start = chunk.origin_offset + local_start
        abs_end = chunk.origin_offset + local_end

        matched_canon, _ = canonicalize(matched)
        value_canon, _ = canonicalize(claim.value)
        if value_canon and value_canon not in matched_canon:
            return VerificationResult(
                verified=False,
                reason="claim.value not derivable from raw_snippet",
                reason_code=REASON_VALUE_NOT_DERIVABLE,
                matched_text=None,
            )

        field_tokens = set(_FIELD_TOK_RE.findall(claim.field_name.lower()))
        is_strict = bool(field_tokens & _STRICT_FIELD_TOKENS)
        if is_strict:
            ratio = len(value_canon) / max(1, len(matched_canon))
            if ratio < self.STRICT_FIELD_RATIO:
                return VerificationResult(
                    verified=False,
                    reason=f"strict field '{claim.field_name}' covers "
                           f"{ratio:.0%} (<90%)",
                    reason_code=REASON_STRICT_THRESHOLD,
                    matched_text=None,
                )
        elif value_canon and len(value_canon) < max(
            2, len(matched_canon) // self.MIN_WEAK_RATIO
        ):
            return VerificationResult(
                verified=False,
                reason="claim.value covers <5% of snippet (weak)",
                reason_code=REASON_WEAK_VALUE,
                matched_text=None,
            )

        if query and value_canon and len(value_canon) >= 3:
            q_canon, _ = canonicalize(query)
            if value_canon in q_canon:
                return VerificationResult(
                    verified=False,
                    reason="claim.value echoes query",
                    reason_code=REASON_QUERY_ECHO,
                    matched_text=None,
                )
            import difflib
            best = max(
                (difflib.SequenceMatcher(None, value_canon, q_tok).ratio()
                 for q_tok in q_canon.split() if q_tok),
                default=0.0,
            )
            if best >= self.QUERY_ECHO_SIMILARITY:
                return VerificationResult(
                    verified=False,
                    reason=f"claim.value {best:.0%} similar to query token",
                    reason_code=REASON_QUERY_SIMILAR,
                    matched_text=None,
                )

        # matched_text minimization: omit when redundant.
        mt: Optional[str] = None
        if matched != claim.raw_snippet and matched != claim.value:
            mt = matched

        return VerificationResult(
            verified=True,
            reason="verbatim match; value derivable from source",
            reason_code=REASON_VERBATIM,
            char_start=abs_start, char_end=abs_end, matched_text=mt,
        )


class AdjudicatorAgent:
    def __init__(self, *, strict: bool = True) -> None:
        self.strict = strict

    def adjudicate(
        self, *, document_id: str,
        claims: List[ExtractedClaim],
        chunks_by_id: Dict[str, Chunk],
        verdicts: List[VerificationResult],
        doc: Optional[DocumentBuffer] = None,
    ) -> VerifiedPayload:
        if not claims:
            raise UnverifiableDataError(
                "No extractable claims",
                {"document_id": document_id, "reason": "zero_verified_claims"},
            )
        if len(claims) != len(verdicts):
            raise UnverifiableDataError("Internal adjudication mismatch")

        rejection_summary: Dict[str, int] = {}
        for c, v in zip(claims, verdicts):
            if not v.verified:
                rejection_summary[v.reason_code] = (
                    rejection_summary.get(v.reason_code, 0) + 1
                )

        failures = [(c, v) for c, v in zip(claims, verdicts) if not v.verified]
        if failures and self.strict:
            raise UnverifiableDataError(
                "Source document does not contain explicit data point",
                {
                    "document_id": document_id,
                    "failures": [
                        {"field": c.field_name, "reason": v.reason,
                         "code": v.reason_code}
                        for c, v in failures
                    ],
                    "rejection_summary": rejection_summary,
                },
            )

        kept = [(c, v) for c, v in zip(claims, verdicts) if v.verified]
        if not kept:
            raise UnverifiableDataError(
                "Source document does not contain explicit data point",
                {"document_id": document_id,
                 "rejection_summary": rejection_summary},
            )

        lineage = LineageIndex()
        for c, v in kept:
            canon_value = canonicalize(c.value)[0]
            lineage.record(c.field_name, canon_value, document_id,
                           c.chunk_id, v.char_start or 0, v.char_end or 0)

        seen: set = set()
        verified: List[VerifiedClaim] = []
        for c, v in kept:
            canon_value = canonicalize(c.value)[0]
            key = (c.field_name, canon_value)
            if key in seen:
                continue
            seen.add(key)
            vc = self._to_verified(
                document_id, chunks_by_id, c, v, lineage,
            )
            ev_count, doc_div = lineage.evidence(c.field_name, canon_value)
            vc.verification.evidence_count = ev_count
            vc.verification.doc_diversity = doc_div
            if ev_count > 1:
                vc.verification.reason = (
                    f"{vc.verification.reason} "
                    f"(corroborated by {ev_count} sources)"
                )
            verified.append(vc)

        payload = VerifiedPayload(
            document_id=document_id, claims=verified, audit_hash="",
            rejection_summary=rejection_summary,
        )

        for vc in payload.claims:
            ch = chunks_by_id.get(vc.coordinate.chunk_id)
            if ch is None:
                raise SchemaViolationError("Post-build: chunk_id missing")
            ls = vc.coordinate.char_start - ch.origin_offset
            le = vc.coordinate.char_end - ch.origin_offset
            if ls < 0 or le > len(ch.text) or le < ls:
                raise SchemaViolationError("Post-build: coordinate out of bounds")
            src_canon, _ = canonicalize(ch.text[ls:le])
            val_canon, _ = canonicalize(vc.value)
            if val_canon and val_canon not in src_canon:
                raise SchemaViolationError(
                    "Post-build: value not derivable from emitted coordinate"
                )

        payload.audit_hash = compute_audit_hash(payload)
        return payload

    @staticmethod
    def _to_verified(document_id: str,
                     chunks_by_id: Dict[str, Chunk],
                     claim: ExtractedClaim,
                     verdict: VerificationResult,
                     lineage: LineageIndex) -> VerifiedClaim:
        chunk = chunks_by_id[claim.chunk_id]
        if verdict.char_start is None or verdict.char_end is None:
            raise SchemaViolationError(
                "verified claim missing precise span; refusing to emit",
                {"field": claim.field_name, "chunk_id": claim.chunk_id},
            )
        coord = SourceCoordinate(
            document_id=document_id,
            chunk_id=claim.chunk_id,
            char_start=verdict.char_start,
            char_end=verdict.char_end,
            bbox=chunk.bbox,
            row_index=chunk.row_index,
            col_index=chunk.col_index,
            json_path=chunk.json_path,
        )
        canon_value = canonicalize(claim.value)[0]
        witnesses = lineage.witnesses(claim.field_name, canon_value)
        provenance: List[SourceCoordinate] = []
        for (doc_id, chunk_id, start, end) in witnesses:
            if doc_id == document_id and chunk_id == claim.chunk_id \
               and start == verdict.char_start and end == verdict.char_end:
                continue
            ch = chunks_by_id.get(chunk_id)
            if ch is None:
                continue
            provenance.append(SourceCoordinate(
                document_id=doc_id,
                chunk_id=chunk_id,
                char_start=start,
                char_end=end,
                bbox=ch.bbox,
                row_index=ch.row_index,
                col_index=ch.col_index,
                json_path=ch.json_path,
            ))
        return VerifiedClaim(
            field_name=intern_str(claim.field_name),
            value=claim.value,
            raw_snippet=claim.raw_snippet,
            coordinate=coord,
            verification=verdict,
            provenance=provenance,
        )


__all__ = [
    "ExtractorAgent", "SkepticAgent", "AdjudicatorAgent",
    "EXTRACTOR_SYSTEM_PROMPT",
]
=======
"""
Layer 2 — Adversarial Multi-Agent Adjudication.
================================================
Agent 1 (Extractor)  — LLM proposer. Untrusted. Uses the EXISTING provider
                       infrastructure (api.providers) and, if no remote provider
                       is available, the existing local twin / hot-swap engine.
Agent 2 (Skeptic)    — deterministic string-match oracle. No LLM. No heuristics.
Agent 3 (Adjudicator) — binary gate. One failed claim ⇒ whole payload rejected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Dict, List, Optional

from pydantic import ValidationError

from .bounding import Chunk, DocumentBuffer, canonicalize
from .schemas import (
    ExtractedClaim,
    NoProviderAvailableError,
    SchemaViolationError,
    SourceCoordinate,
    UnverifiableDataError,
    VerificationResult,
    VerifiedClaim,
    VerifiedPayload,
    compute_audit_hash,
)

log = logging.getLogger("axelr.ahe.agents")


# ═══════════════════════════════════════════════════════════════════════════
# Agent 1 — Extractor (LLM proposer, integrated with api.providers)
# ═══════════════════════════════════════════════════════════════════════════
EXTRACTOR_SYSTEM_PROMPT = """\
You are Axelr's EXTRACTION AGENT. You are an UNTRUSTED PROPOSER inside a
zero-hallucination pipeline. Your output is NOT shown to the user; it is
cross-examined by a deterministic string-matcher and rejected if ANY claim
fails to match the source character-for-character.

ABSOLUTE RULES (a single violation rejects the whole payload):
1. Return ONLY valid JSON. No prose. No markdown fences. No commentary.
2. The JSON must be: {"claims": [ ... ]}.
3. Each claim MUST have EXACTLY these keys:
     "field_name"  : one of the requested target fields (verbatim)
     "value"       : the extracted value, as a string
     "raw_snippet" : the EXACT, VERBATIM, CONTIGUOUS substring of the source
                     chunk from which "value" is derived. Do NOT paraphrase,
                     normalise, reformat, truncate, or insert ellipses.
     "chunk_id"    : the chunk_id containing that snippet.
4. If a requested field is NOT verbatim-present in any chunk, OMIT it entirely.
   Never invent, infer, extrapolate, or default.
5. Do NOT include any key beyond the four above on a claim.
6. Numeric formatting must match the source EXACTLY (e.g. "1,234.50" not
   "1234.5"). Currency symbols, units, punctuation are part of the value.
"""


class ExtractorAgent:
    def __init__(self, *, timeout_s: float = 60.0,
                 max_output_tokens: int = 4096,
                 max_self_correction_retries: int = 2) -> None:
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens
        self.max_self_correction_retries = max_self_correction_retries

    async def extract(
        self, *, query: str, target_fields: List[str], chunks: List[Chunk],
        workspace: str, tier: str, correction_hint: Optional[str] = None,
    ) -> List[ExtractedClaim]:
        user_prompt = self._build_user_prompt(
            query, target_fields, chunks, correction_hint,
        )
        raw = await self._call_llm(EXTRACTOR_SYSTEM_PROMPT, user_prompt,
                                   workspace=workspace, tier=tier)
        return self._parse(raw, target_fields)

    # ── prompt ────────────────────────────────────────────────────────────
    @staticmethod
    def _build_user_prompt(query, target_fields, chunks, correction_hint):
        payload = [
            {
                "chunk_id": c.chunk_id,
                "text": c.text,
                "coordinate": {
                    "row_index": c.coordinate.row_index,
                    "col_index": c.coordinate.col_index,
                    "json_path": c.coordinate.json_path,
                    "page": c.coordinate.bbox.page if c.coordinate.bbox else None,
                },
            }
            for c in chunks
        ]
        hint = ""
        if correction_hint:
            hint = (
                "\n\nPREVIOUS ATTEMPT FAILED. FIX AND RETRY:\n"
                f"<<<{correction_hint}>>>\n"
            )
        return (
            f"TARGET FIELDS (extract each ONLY if verbatim-present):\n"
            f"{json.dumps(target_fields)}\n\n"
            f"USER QUERY:\n{query}\n\n"
            f"SOURCE CHUNKS (each has an id; snippets MUST come from one):\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n"
            f"{hint}\n"
            f"Return ONLY the JSON object {{\"claims\": [...]}}."
        )

    # ── transport: reuse api.providers + twin engine fallback ─────────────
    async def _call_llm(self, system: str, user: str,
                        *, workspace: str, tier: str) -> str:
        """
        Uses the SAME provider infrastructure as the rest of the platform:
          1. get_dynamically_ranked_providers(workspace)
          2. PROVIDER_FUNC_MAP + PROVIDER_KEY_CHECK + PROVIDER_MODELS
          3. Local twin engine as last resort
        """
        try:
            from api.providers import (  # type: ignore
                PROVIDER_FUNC_MAP,
                PROVIDER_KEY_CHECK,
                PROVIDER_MODELS,
                get_dynamically_ranked_providers,
                record_provider_result,
            )
        except Exception as e:                                     # noqa: BLE001
            log.warning("providers_unavailable error=%s", e)
            return await self._local_fallback(system, user, workspace)

        full_prompt = f"{system}\n\n{user}"
        ranked = await get_dynamically_ranked_providers(workspace)

        for provider_name in ranked:
            if provider_name == "local":
                continue
            if not PROVIDER_KEY_CHECK.get(provider_name, False):
                continue
            func = PROVIDER_FUNC_MAP.get(provider_name)
            models = PROVIDER_MODELS.get(provider_name) or []
            if not func or not models:
                continue
            model = models[0]
            try:
                resp = await asyncio.wait_for(
                    func(full_prompt, self.max_output_tokens, 0.0, model),
                    timeout=self.timeout_s,
                )
                if resp and resp.strip():
                    await record_provider_result(provider_name, 0.0, success=True)
                    return resp
            except Exception as e:                                 # noqa: BLE001
                log.debug("extractor_provider_failed provider=%s error=%s",
                          provider_name, e)
                await record_provider_result(provider_name, 0.0, success=False)
                continue

        return await self._local_fallback(system, user, workspace)

    async def _local_fallback(self, system: str, user: str,
                              workspace: str) -> str:
        try:
            from core.twin_engine import execute_local_inference  # type: ignore
            text = await asyncio.wait_for(
                execute_local_inference(workspace, "extraction",
                                        f"{system}\n\n{user}"),
                timeout=self.timeout_s,
            )
            if text and not text.lstrip().startswith("[stub]"):
                return text
        except Exception as e:                                     # noqa: BLE001
            log.debug("local_fallback_failed error=%s", e)
        raise NoProviderAvailableError(
            "No provider (remote or local) produced a response for the extractor."
        )

    # ── parse / enforce ───────────────────────────────────────────────────
    @staticmethod
    def _strip_fences(s: str) -> str:
        s = s.strip()
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z0-9]*\n", "", s)
            s = re.sub(r"\n```\s*$", "", s)
        return s

    def _parse(self, raw: str, target_fields: List[str]) -> List[ExtractedClaim]:
        cleaned = self._strip_fences(raw)
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise SchemaViolationError(
                f"LLM did not return valid JSON: {e.msg}",
                {"raw_preview": raw[:512]},
            )
        if not isinstance(obj, dict) or not isinstance(obj.get("claims"), list):
            raise SchemaViolationError(
                "LLM output must be {'claims': [...]}",
                {"raw_preview": raw[:512]},
            )
        out: List[ExtractedClaim] = []
        for i, c in enumerate(obj["claims"]):
            if not isinstance(c, dict):
                raise SchemaViolationError(f"claims[{i}] is not an object")
            try:
                claim = ExtractedClaim.model_validate(c)
            except ValidationError as e:
                raise SchemaViolationError(
                    f"claims[{i}] violated schema",
                    {"errors": e.errors(include_url=False)},
                ) from e
            if claim.field_name not in target_fields:
                log.warning("dropping claim for un-requested field %r",
                            claim.field_name)
                continue
            out.append(claim)
        return out


# ═══════════════════════════════════════════════════════════════════════════
# Agent 2 — Skeptic (deterministic string-match oracle)
# ═══════════════════════════════════════════════════════════════════════════
class SkepticAgent:
    """
    No LLM. No heuristics. Pure deterministic string matching.
    Every claim must survive ALL of:
      (1) chunk_id exists
      (2) canonicalized raw_snippet is a contiguous substring of canonicalized chunk
      (3) canonicalized claim.value is a subsequence of the matched region
    """

    def cross_examine(self, claim: ExtractedClaim,
                      chunks_by_id: Dict[str, Chunk],
                      doc: DocumentBuffer) -> VerificationResult:
        chunk = chunks_by_id.get(claim.chunk_id)
        if chunk is None:
            return VerificationResult(
                verified=False,
                reason=f"chunk_id '{claim.chunk_id}' not found in source document",
            )

        chunk_canon, chunk_map = canonicalize(chunk.text)
        snippet_canon, _ = canonicalize(claim.raw_snippet)
        if not snippet_canon:
            return VerificationResult(
                verified=False,
                reason="empty raw_snippet after canonicalization",
            )

        pos = chunk_canon.find(snippet_canon)
        if pos < 0:
            return VerificationResult(
                verified=False,
                reason=("raw_snippet is not a verbatim substring of the "
                        "referenced chunk (character-level mismatch)"),
            )

        local_start = chunk_map[pos]
        local_end = chunk_map[pos + len(snippet_canon) - 1] + 1
        abs_start = chunk.origin_offset + local_start
        abs_end = chunk.origin_offset + local_end
        matched_original = doc.full_text[abs_start:abs_end]

        matched_canon, _ = canonicalize(matched_original)
        value_canon, _ = canonicalize(claim.value)
        if value_canon and value_canon not in matched_canon:
            return VerificationResult(
                verified=False,
                reason=("claim.value is not derivable from raw_snippet "
                        "(contains tokens absent from the matched source text)"),
                matched_text=matched_original,
            )

        return VerificationResult(
            verified=True,
            reason="verbatim match; value fully derivable from matched source",
            char_start=abs_start,
            char_end=abs_end,
            matched_text=matched_original,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Agent 3 — Adjudicator (binary gate)
# ═══════════════════════════════════════════════════════════════════════════
class AdjudicatorAgent:
    def adjudicate(
        self, *, document_id: str,
        claims: List[ExtractedClaim],
        chunks_by_id: Dict[str, Chunk],
        verdicts: List[VerificationResult],
    ) -> VerifiedPayload:
        if not claims:
            raise UnverifiableDataError(
                "No extractable claims; source document does not contain explicit data point",
                {"document_id": document_id, "reason": "zero_verified_claims"},
            )
        if len(claims) != len(verdicts):
            raise UnverifiableDataError("Internal adjudication mismatch")

        failures = [
            {"field": c.field_name, "reason": v.reason}
            for c, v in zip(claims, verdicts) if not v.verified
        ]
        if failures:
            raise UnverifiableDataError(
                "Source document does not contain explicit data point",
                {"document_id": document_id, "failures": failures},
            )

        verified: List[VerifiedClaim] = []
        for claim, verdict in zip(claims, verdicts):
            chunk = chunks_by_id[claim.chunk_id]
            coord = SourceCoordinate(
                document_id=document_id,
                chunk_id=chunk.chunk_id,
                char_start=verdict.char_start or 0,
                char_end=verdict.char_end or 0,
                bbox=chunk.coordinate.bbox,
                row_index=chunk.coordinate.row_index,
                col_index=chunk.coordinate.col_index,
                json_path=chunk.coordinate.json_path,
            )
            verified.append(VerifiedClaim(
                field_name=claim.field_name,
                value=claim.value,
                raw_snippet=claim.raw_snippet,
                coordinate=coord,
                verification=verdict,
            ))

        payload = VerifiedPayload(
            document_id=document_id,
            claims=verified,
            audit_hash="",  # recomputed below
        )
        payload.audit_hash = compute_audit_hash(payload)
        return payload


__all__ = ["ExtractorAgent", "SkepticAgent", "AdjudicatorAgent",
           "EXTRACTOR_SYSTEM_PROMPT"]
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
