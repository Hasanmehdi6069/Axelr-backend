<<<<<<< HEAD
"""Layer 3 — Anti-Hallucination Engine Orchestrator (v1.5).

Adds: async streaming iterator, self-diagnose, deterministic finalization,
invariant self-check. Preserves all v1.4 behavior.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Optional

from .agents import AdjudicatorAgent, ExtractorAgent, SkepticAgent
from .bounding import Chunk, DocumentBuffer, DocumentIngestor, canonicalize
from ._verify import (
    balance_tags, ByteTokenizer, matches_shape,
    pda_balanced, json_fsa_valid, py_undefined_names, cross_language_leak,
    check_arithmetic, negation_window, contradiction,
    request_fingerprint, replay_key, chunk_hash, sign_root, project_budget,
    stable_tie_break, verify_invariants,
    REASON_NEGATED,
)
from ._runtime import (
    build_audit, conformance_score, bitemporal_stamp,
    QuotaTracker, TokenCaps, intern_str, CircuitBreaker,
)
from .schemas import (
    DocumentParseError, ExtractionRequest, SchemaViolationError,
    UnverifiableDataError, VerifiedClaim, VerifiedPayload, compute_audit_hash,
)

log = logging.getLogger("axelr.ahe.engine")


@dataclass(frozen=True)
class TierBudget:
    max_chunk_chars: int
    max_claims: int
    retries: int
    allow_llm: bool
    max_files_per_prompt: int
    monthly_queries: int
    monthly_mb: int


TIER_BUDGET: Dict[str, TierBudget] = {
    intern_str("free"):       TierBudget( 800,  50, 0, False, 10,  15, 15),
    intern_str("guest"):      TierBudget( 800,  50, 0, False, 10,  15, 15),
    intern_str("pro"):        TierBudget(1400, 200, 1, True,  30,  70, 30),
    intern_str("business"):   TierBudget(1400, 500, 2, True,  70, 140, 60),
    intern_str("enterprise"): TierBudget(2000, 1000, 3, True, 999, -1, -1),
}


class AntiHallucinationEngine:
    def __init__(
        self, *,
        extractor_timeout_s: float = 60.0,
        default_max_chunk_chars: int = 1400,
        adjudicator_strict: bool = True,
        quota: Optional[QuotaTracker] = None,
        chunk_batch_size: int = 25,
        emit_chunk_hashes: bool = False,
        signing_secret: Optional[bytes] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self.extractor_timeout_s = extractor_timeout_s
        self.default_max_chunk_chars = default_max_chunk_chars
        self.skeptic = SkepticAgent()
        self.adjudicator = AdjudicatorAgent(strict=adjudicator_strict)
        self.quota = quota or QuotaTracker()
        self.chunk_batch_size = max(1, chunk_batch_size)
        self.emit_chunk_hashes = emit_chunk_hashes
        self.signing_secret = signing_secret
        self.circuit_breaker = circuit_breaker or CircuitBreaker()

    # ── Non-streaming full-run ───────────────────────────────────────────
    async def run(self, req: ExtractionRequest,
                  raw_bytes: bytes) -> VerifiedPayload:
        t0 = time.perf_counter()
        budget = TIER_BUDGET.get(req.tier.lower())
        if budget is None:
            raise SchemaViolationError(f"Unknown tier {req.tier!r}")

        self._preflight(req, budget, raw_bytes)
        if req.enforce_quota:
            self.quota.check_and_consume(
                workspace=req.workspace, tier=req.tier,
                mb_used=len(raw_bytes) / (1024 * 1024),
                max_queries=budget.monthly_queries,
                max_mb=budget.monthly_mb,
            )
        deadline = t0 + req.pipeline_timeout_s
        ingestor = DocumentIngestor(max_chunk_chars=budget.max_chunk_chars)
        doc = ingestor.ingest(req, raw_bytes)
        chunks_by_id = {c.chunk_id: c for c in doc.chunks}
        if not doc.chunks:
            raise UnverifiableDataError(
                "Source document contains no extractable text",
                {"document_id": req.document_id},
            )

        self._pre_checks(req, raw_bytes)

        extractor = self._make_extractor(budget)
        claims, correction_hint, last_err = [], None, None
        for attempt in range(budget.retries + 1):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise UnverifiableDataError(
                    "pipeline_timeout", {"stage": "pre_extract"},
                )
            try:
                claims = await asyncio.wait_for(
                    extractor.extract(
                        query=req.query, target_fields=req.target_fields,
                        chunks=doc.chunks, workspace=req.workspace,
                        tier=req.tier, correction_hint=correction_hint,
                    ),
                    timeout=remaining,
                )
                last_err = None
                break
            except SchemaViolationError as e:
                last_err = e
                correction_hint = (
                    f"{e.message} :: details={json.dumps(e.details)[:400]}"
                )
                log.warning("extractor_schema_violation attempt=%d error=%s",
                            attempt + 1, e.message)
        if last_err is not None:
            raise last_err
        if len(claims) > budget.max_claims:
            raise SchemaViolationError(
                f"Claim count {len(claims)} exceeds tier limit {budget.max_claims}")

        caps = TokenCaps(per_field=1024, global_=budget.max_claims * 1024)
        total = 0
        for c in claims:
            caps.check_field(c.field_name, c.value)
            total += len(c.value)
        caps.check_total(total)

        verdicts = self._verify_claims(claims, chunks_by_id, doc, req)

        payload = self.adjudicator.adjudicate(
            document_id=req.document_id, claims=claims,
            chunks_by_id=chunks_by_id, verdicts=verdicts,
        )

        if self.emit_chunk_hashes:
            payload.chunk_hashes = {
                c.chunk_id: c.hash16() for c in doc.chunks
            }

        self._finalize_payload(payload, req, raw_bytes)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        log.info(
            "verified document_id=%s tier=%s claims=%d score=%d "
            "audit=%s elapsed_ms=%.1f",
            req.document_id, req.tier, len(payload.claims),
            payload.conformance_score, payload.merkle_root[:12], elapsed_ms,
        )
        return payload

    # ── Batched streaming full-run ───────────────────────────────────────
    async def run_streaming(self, req: ExtractionRequest,
                            raw_bytes: bytes) -> VerifiedPayload:
        t0 = time.perf_counter()
        budget = TIER_BUDGET.get(req.tier.lower())
        if budget is None:
            raise SchemaViolationError(f"Unknown tier {req.tier!r}")

        self._preflight(req, budget, raw_bytes)
        if req.enforce_quota:
            self.quota.check_and_consume(
                workspace=req.workspace, tier=req.tier,
                mb_used=len(raw_bytes) / (1024 * 1024),
                max_queries=budget.monthly_queries,
                max_mb=budget.monthly_mb,
            )
        deadline = t0 + req.pipeline_timeout_s
        ingestor = DocumentIngestor(max_chunk_chars=budget.max_chunk_chars)
        extractor = self._make_extractor(budget)

        verified: List[VerifiedClaim] = []
        seen_keys: Dict[tuple, int] = {}
        verified_chunks: Dict[str, Chunk] = {}
        caps = TokenCaps(per_field=1024, global_=budget.max_claims * 1024)
        total_chars = 0
        doc_stub = DocumentBuffer(chunks=[], adversarial_counts={})
        rejection_summary: Dict[str, int] = {}

        async def process_batch(batch: List[Chunk]):
            nonlocal total_chars
            if not batch:
                return
            chunk_map = {c.chunk_id: c for c in batch}
            try:
                claims = await asyncio.wait_for(
                    extractor.extract(
                        query=req.query, target_fields=req.target_fields,
                        chunks=batch, workspace=req.workspace, tier=req.tier,
                    ),
                    timeout=max(0.1, deadline - time.perf_counter()),
                )
            except SchemaViolationError:
                return
            for c in claims:
                if len(verified) >= budget.max_claims:
                    return
                caps.check_field(c.field_name, c.value)
                total_chars += len(c.value)
                v = self.skeptic.cross_examine(
                    c, chunk_map, doc_stub, req.query,
                )
                if not v.verified:
                    rejection_summary[v.reason_code] = (
                        rejection_summary.get(v.reason_code, 0) + 1
                    )
                    continue
                s0, s1, neg = negation_window(
                    chunk_map[c.chunk_id].text, v.char_start, v.char_end,
                )
                if neg and contradiction(
                    c.value, chunk_map[c.chunk_id].text[s0:s1],
                ):
                    rejection_summary[REASON_NEGATED] = (
                        rejection_summary.get(REASON_NEGATED, 0) + 1
                    )
                    continue
                shape = req.value_shapes.get(c.field_name)
                if shape and not matches_shape(c.value, shape):
                    raise SchemaViolationError(
                        f"value for '{c.field_name}' does not match shape "
                        f"'{shape}'",
                        {"value": c.value, "shape": shape},
                    )
                canon_value = canonicalize(c.value)[0]
                key = (c.field_name, canon_value)
                if key in seen_keys:
                    verified[seen_keys[key]].verification.evidence_count += 1
                    continue
                from ._verify import LineageIndex
                li = LineageIndex()
                li.record(c.field_name, canon_value, req.document_id,
                          c.chunk_id, v.char_start, v.char_end)
                vc = self.adjudicator._to_verified(
                    req.document_id, chunk_map, c, v, li,
                )
                seen_keys[key] = len(verified)
                verified.append(vc)
                verified_chunks[c.chunk_id] = chunk_map[c.chunk_id]

        batch: List[Chunk] = []
        for chunk in ingestor.iter_chunks(req, raw_bytes):
            if len(verified) >= budget.max_claims:
                break
            if time.perf_counter() > deadline:
                raise UnverifiableDataError(
                    "pipeline_timeout", {"stage": "streaming"},
                )
            batch.append(chunk)
            if len(batch) >= self.chunk_batch_size:
                await process_batch(batch)
                batch = []
        if batch:
            await process_batch(batch)

        caps.check_total(total_chars)
        if not verified:
            raise UnverifiableDataError(
                "Source document does not contain explicit data point",
                {"document_id": req.document_id,
                 "rejection_summary": rejection_summary},
            )

        payload = VerifiedPayload(
            document_id=req.document_id, claims=verified, audit_hash="",
            rejection_summary=rejection_summary,
        )

        for vc in payload.claims:
            ch = verified_chunks.get(vc.coordinate.chunk_id)
            if ch is None:
                raise SchemaViolationError("post-build: chunk_id missing")
            ls = vc.coordinate.char_start - ch.origin_offset
            le = vc.coordinate.char_end - ch.origin_offset
            if ls < 0 or le > len(ch.text) or le < ls:
                raise SchemaViolationError("post-build: coordinate out of bounds")
            src_canon, _ = canonicalize(ch.text[ls:le])
            val_canon, _ = canonicalize(vc.value)
            if val_canon and val_canon not in src_canon:
                raise SchemaViolationError(
                    "post-build: value not derivable from emitted coordinate"
                )

        if self.emit_chunk_hashes:
            payload.chunk_hashes = {
                cid: ch.hash16() for cid, ch in verified_chunks.items()
            }

        self._finalize_payload(payload, req, raw_bytes)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        log.info(
            "verified_streaming document_id=%s tier=%s claims=%d score=%d "
            "audit=%s elapsed_ms=%.1f",
            req.document_id, req.tier, len(payload.claims),
            payload.conformance_score, payload.merkle_root[:12], elapsed_ms,
        )
        return payload

    # ── Async streaming iterator (new in v1.5) ───────────────────────────
    async def run_streaming_iter(
        self, req: ExtractionRequest, raw_bytes: bytes,
    ) -> AsyncIterator[VerifiedClaim]:
        """Yield verified claims one at a time. Consumer may break early.

        No full payload is returned. Use .run_streaming() if you need the
        aggregated VerifiedPayload (with signature, conformance score, etc.).
        """
        budget = TIER_BUDGET.get(req.tier.lower())
        if budget is None:
            raise SchemaViolationError(f"Unknown tier {req.tier!r}")
        self._preflight(req, budget, raw_bytes)
        if req.enforce_quota:
            self.quota.check_and_consume(
                workspace=req.workspace, tier=req.tier,
                mb_used=len(raw_bytes) / (1024 * 1024),
                max_queries=budget.monthly_queries,
                max_mb=budget.monthly_mb,
            )
        deadline = time.perf_counter() + req.pipeline_timeout_s
        ingestor = DocumentIngestor(max_chunk_chars=budget.max_chunk_chars)
        extractor = self._make_extractor(budget)

        seen_keys: Dict[tuple, int] = {}
        seen_count = 0
        total_chars = 0
        caps = TokenCaps(per_field=1024, global_=budget.max_claims * 1024)
        doc_stub = DocumentBuffer(chunks=[], adversarial_counts={})

        async def process_batch(batch: List[Chunk]):
            nonlocal total_chars, seen_count
            if not batch:
                return
            chunk_map = {c.chunk_id: c for c in batch}
            try:
                claims = await asyncio.wait_for(
                    extractor.extract(
                        query=req.query, target_fields=req.target_fields,
                        chunks=batch, workspace=req.workspace, tier=req.tier,
                    ),
                    timeout=max(0.1, deadline - time.perf_counter()),
                )
            except SchemaViolationError:
                return
            for c in claims:
                if seen_count >= budget.max_claims:
                    return
                caps.check_field(c.field_name, c.value)
                total_chars += len(c.value)
                v = self.skeptic.cross_examine(
                    c, chunk_map, doc_stub, req.query,
                )
                if not v.verified:
                    continue
                s0, s1, neg = negation_window(
                    chunk_map[c.chunk_id].text, v.char_start, v.char_end,
                )
                if neg and contradiction(
                    c.value, chunk_map[c.chunk_id].text[s0:s1],
                ):
                    continue
                shape = req.value_shapes.get(c.field_name)
                if shape and not matches_shape(c.value, shape):
                    raise SchemaViolationError(
                        f"value for '{c.field_name}' does not match shape "
                        f"'{shape}'",
                        {"value": c.value, "shape": shape},
                    )
                canon_value = canonicalize(c.value)[0]
                key = (c.field_name, canon_value)
                if key in seen_keys:
                    continue
                from ._verify import LineageIndex
                li = LineageIndex()
                li.record(c.field_name, canon_value, req.document_id,
                          c.chunk_id, v.char_start, v.char_end)
                vc = self.adjudicator._to_verified(
                    req.document_id, chunk_map, c, v, li,
                )
                seen_keys[key] = seen_count
                seen_count += 1
                yield vc

        batch: List[Chunk] = []
        for chunk in ingestor.iter_chunks(req, raw_bytes):
            if seen_count >= budget.max_claims:
                break
            if time.perf_counter() > deadline:
                raise UnverifiableDataError(
                    "pipeline_timeout", {"stage": "streaming_iter"},
                )
            batch.append(chunk)
            if len(batch) >= self.chunk_batch_size:
                async for vc in process_batch(batch):
                    yield vc
                batch = []
        if batch:
            async for vc in process_batch(batch):
                yield vc

        caps.check_total(total_chars)

    # ── Self-diagnostic (new in v1.5) ────────────────────────────────────
    def self_diagnose(self) -> dict:
        report: dict = {
            "engine_version": "axelr-ahe/1.5",
            "adjudicator_strict": self.adjudicator.strict,
            "chunk_batch_size": self.chunk_batch_size,
            "emit_chunk_hashes": self.emit_chunk_hashes,
            "signed": self.signing_secret is not None,
            "tiers": sorted(TIER_BUDGET.keys()),
            "circuit_breaker": self.circuit_breaker.state(),
        }
        try:
            from ._verify import MinimalPerfectHash
            m = MinimalPerfectHash([b"a", b"b", b"c"])
            report["mph_ok"] = (b"a" in m) and (b"z" not in m)
        except Exception as e:  # noqa: BLE001
            report["mph_ok"] = False
            report["mph_error"] = str(e)
        try:
            from .bounding import canonicalize as _c
            report["canon_ok"] = _c("A B")[0] == "A B"
        except Exception as e:  # noqa: BLE001
            report["canon_ok"] = False
            report["canon_error"] = str(e)
        try:
            from ._runtime import merkle_root
            report["merkle_ok"] = (
                merkle_root([b"a", b"b"]) == merkle_root([b"a", b"b"])
            )
        except Exception as e:  # noqa: BLE001
            report["merkle_ok"] = False
            report["merkle_error"] = str(e)
        report["healthy"] = all(
            report.get(k) for k in ("mph_ok", "canon_ok", "merkle_ok")
        )
        return report

    # ── Helpers ──────────────────────────────────────────────────────────
    def _make_extractor(self, budget: TierBudget) -> ExtractorAgent:
        return ExtractorAgent(
            timeout_s=self.extractor_timeout_s,
            max_self_correction_retries=budget.retries,
            allow_llm=budget.allow_llm,
            circuit_breaker=self.circuit_breaker,
        )

    @staticmethod
    def _preflight(req: ExtractionRequest, budget: TierBudget,
                   raw_bytes: bytes) -> None:
        if len(raw_bytes) > req.max_document_bytes:
            raise DocumentParseError(
                f"Document exceeds max_document_bytes={req.max_document_bytes}",
                {"actual_bytes": len(raw_bytes)},
            )
        proj = project_budget(
            max_chunks=req.max_chunks,
            max_claims=budget.max_claims,
            avg_chunk_chars=budget.max_chunk_chars,
        )
        if proj["total_bytes"] > req.max_memory_bytes:
            raise DocumentParseError(
                "Request exceeds max_memory_bytes (pre-flight projection)",
                {
                    "projected_bytes": proj["total_bytes"],
                    "limit": req.max_memory_bytes,
                    "assumptions": proj["assumptions"],
                },
            )

    @staticmethod
    def _pre_checks(req: ExtractionRequest, raw_bytes: bytes) -> None:
        dt = req.document_type
        if dt == "html":
            ok, why = balance_tags(raw_bytes)
            if not ok:
                raise DocumentParseError(f"HTML not balanced: {why}")
        elif dt in ("py", "js"):
            ok, why = pda_balanced(raw_bytes)
            if not ok:
                raise DocumentParseError(f"bracket mismatch: {why}")
            if dt == "py":
                u = py_undefined_names(
                    raw_bytes.decode("utf-8", "replace"),
                )
                if u:
                    raise SchemaViolationError(
                        "undefined names in generated code",
                        {"names": u[:20]},
                    )
            elif req.code_dialect:
                tokens = [bytes(raw_bytes[s:e])
                          for k, s, e in ByteTokenizer(raw_bytes) if k == 1]
                leaked = cross_language_leak(tokens, req.code_dialect)
                if leaked:
                    raise SchemaViolationError(
                        "dialect leak",
                        {"tokens": [t.decode('ascii', 'replace')
                                    for t in leaked[:10]]},
                    )
        elif dt == "json":
            ok, why = json_fsa_valid(raw_bytes)
            if not ok:
                raise DocumentParseError(f"json structure: {why}")
        elif dt == "text":
            bad = [eq for ok, eq, _ in
                   check_arithmetic(
                       raw_bytes.decode("utf-8", "replace"),
                   ) if not ok]
            if bad:
                raise SchemaViolationError(
                    "arithmetic mismatch in source",
                    {"equations": bad[:10]},
                )

    def _verify_claims(self, claims, chunks_by_id, doc, req):
        verdicts = [
            self.skeptic.cross_examine(
                c, chunks_by_id, doc, query=req.query,
            )
            for c in claims
        ]
        for c, v in zip(claims, verdicts):
            if not v.verified or v.char_start is None:
                continue
            s0, s1, neg = negation_window(
                chunks_by_id[c.chunk_id].text, v.char_start, v.char_end,
            )
            if neg and contradiction(
                c.value, chunks_by_id[c.chunk_id].text[s0:s1],
            ):
                v.verified = False
                v.reason = "source negated; claim dropped negator (L25)"
                v.reason_code = REASON_NEGATED
                v.negated = True
        for c in claims:
            shape = req.value_shapes.get(c.field_name)
            if shape and not matches_shape(c.value, shape):
                raise SchemaViolationError(
                    f"value for '{c.field_name}' does not match shape "
                    f"'{shape}'",
                    {"value": c.value, "shape": shape},
                )
        return verdicts

    def _finalize_payload(self, payload: VerifiedPayload,
                          req: ExtractionRequest,
                          raw_bytes: bytes) -> None:
        # Deterministic finalization order — bit-for-bit reproducible.
        payload.claims = stable_tie_break(payload.claims)
        payload.audit_hash = compute_audit_hash(payload)
        payload.merkle_root, payload.crc32 = build_audit(payload)

        asserted_at, asserted_mono = bitemporal_stamp()
        payload.asserted_at = asserted_at
        payload.asserted_monotonic = asserted_mono
        finalized_at, _ = bitemporal_stamp()
        payload.finalized_at = finalized_at

        payload.request_fingerprint = request_fingerprint(
            document_bytes=raw_bytes,
            query=req.query,
            target_fields=req.target_fields,
            tier=req.tier,
        )
        payload.replay_key = replay_key(
            request_fingerprint=payload.request_fingerprint,
            engine_version=payload.engine_version,
            canon_version=payload.canon_version,
        )
        payload.signature = sign_root(
            payload.merkle_root, req.workspace, self.signing_secret,
        )

        verified_n = sum(1 for c in payload.claims if c.verification.verified)
        evidence_mean = (
            sum(c.verification.evidence_count for c in payload.claims) /
            max(1, len(payload.claims))
        )
        neg_clean = (
            sum(1 for c in payload.claims if not c.verification.negated) /
            max(1, len(payload.claims))
        )
        multi_doc = (
            sum(1 for c in payload.claims if c.verification.doc_diversity > 1) /
            max(1, len(payload.claims))
        )
        payload.conformance_score = conformance_score(
            verified=verified_n, total=len(payload.claims),
            evidence_mean=evidence_mean, negation_clean_ratio=neg_clean,
            canon_ratio=1.0, multi_doc_ratio=multi_doc,
        )

        # Post-finalization invariant check. Non-invasive; raises if broken.
        violations = verify_invariants(payload)
        if violations:
            raise SchemaViolationError(
                "Post-finalization invariant violation",
                {"violations": violations[:5]},
            )
=======
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
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
