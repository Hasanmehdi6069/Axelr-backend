"""
Layer 3 — Anti-Hallucination Engine Orchestrator.
==================================================
Verify-Before-Output. Wires Agents 1→2→3, enforces the Pydantic schema at
every hop, and runs a bounded self-correction loop on SCHEMA violations only.
(A wrong value is NEVER retried: coercion is not verification.)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .agents import AdjudicatorAgent, ExtractorAgent, SkepticAgent
from .bounding import DocumentIngestor
from .schemas import (
    ExtractionRequest,
    SchemaViolationError,
    UnverifiableDataError,
    VerifiedPayload,
)

log = logging.getLogger("axelr.ahe.engine")


class AntiHallucinationEngine:
    def __init__(
        self, *,
        max_self_correction_retries: int = 2,
        extractor_timeout_s: float = 60.0,
        max_chunk_chars: int = 1400,
        max_claims: int = 500,
    ) -> None:
        self.ingestor = DocumentIngestor(max_chunk_chars=max_chunk_chars)
        self.extractor = ExtractorAgent(
            timeout_s=extractor_timeout_s,
            max_self_correction_retries=max_self_correction_retries,
        )
        self.skeptic = SkepticAgent()
        self.adjudicator = AdjudicatorAgent()
        self.max_self_correction_retries = max_self_correction_retries
        self.max_claims = max_claims

    async def run(self, req: ExtractionRequest, raw_bytes: bytes) -> VerifiedPayload:
        t0 = time.perf_counter()

        # ── Layer 1: deterministic ingestion & bounding ────────────────────
        doc = self.ingestor.ingest(req, raw_bytes)
        chunks_by_id = {c.chunk_id: c for c in doc.chunks}
        log.info("ingest document_id=%s chunks=%d bytes=%d",
                 req.document_id, len(doc.chunks), len(raw_bytes))
        if not doc.chunks:
            raise UnverifiableDataError(
                "Source document contains no extractable text",
                {"document_id": req.document_id},
            )

        # ── Layer 2a: extractor with schema-enforced self-correction ──────
        claims = []
        correction_hint: Optional[str] = None
        last_err: Optional[Exception] = None
        for attempt in range(self.max_self_correction_retries + 1):
            try:
                claims = await self.extractor.extract(
                    query=req.query,
                    target_fields=req.target_fields,
                    chunks=doc.chunks,
                    workspace=req.workspace,
                    tier=req.tier,
                    correction_hint=correction_hint,
                )
                last_err = None
                break
            except SchemaViolationError as e:
                last_err = e
                correction_hint = (
                    f"{e.message} :: details="
                    f"{__import__('json').dumps(e.details)[:400]}"
                )
                log.warning("extractor schema violation attempt=%d error=%s",
                            attempt + 1, e.message)
        if last_err is not None:
            raise last_err

        if len(claims) > self.max_claims:
            raise SchemaViolationError(
                f"Claim count {len(claims)} exceeds limit {self.max_claims}"
            )

        # ── Layer 2b: skeptic cross-examination ────────────────────────────
        verdicts = [self.skeptic.cross_examine(c, chunks_by_id, doc)
                    for c in claims]

        # ── Layer 2c: adjudicator binary gate ──────────────────────────────
        payload = self.adjudicator.adjudicate(
            document_id=req.document_id,
            claims=claims,
            chunks_by_id=chunks_by_id,
            verdicts=verdicts,
        )

        log.info("verified document_id=%s claims=%d audit=%s elapsed_ms=%.1f",
                 req.document_id, len(payload.claims), payload.audit_hash[:12],
                 (time.perf_counter() - t0) * 1000.0)
        return payload


__all__ = ["AntiHallucinationEngine"]
