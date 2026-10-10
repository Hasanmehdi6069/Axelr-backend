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
