# AXELR Anti-Hallucination Engine (AHE)

**Version**: v1.7  
**Module**: `core/anti_hallucination/`  
**Status**: Production-ready, multi-agent verification pipeline  
**Last Updated**: v1.7 fixed v1.5 trailing duplicate syntax error, preserved all v1.6 dispatch/health/batch features

---

## Overview

The Anti-Hallucination Engine is AXELR's standalone verification system that validates extracted claims against source documents. It uses a multi-agent pipeline with tiered budgets, chunked document processing, and mathematical invariant checks to ensure every extracted claim is directly derivable from the source material. This is not a "hallucination detection" model — it is a deterministic verification system that cross-examines claims against their source text using explicit coordinate matching, negation window analysis, and conformance scoring.

---

## Core Architecture

The AHE pipeline consists of three sequential agent stages:

```
Document Ingestion → ExtractorAgent → SkepticAgent → AdjudicatorAgent → VerifiedPayload
```

Each stage operates independently with explicit failure modes and retry policies determined by the user's tier level.

---

## Tiered Budget Configuration

All AHE operations are governed by a strict tiered budget system. No operation can exceed the limits defined for the user's tier.

| Tier | Max Chunk Chars | Max Claims | Retries | Allow LLM | Max Files | Monthly Queries | Monthly MB |
|------|-----------------|------------|---------|-----------|-----------|-----------------|------------|
| Free/Guest | 800 | 50 | 0 | No | 10 | 15 | 15 |
| Pro | 1400 | 200 | 1 | Yes | 30 | 70 | 30 |
| Business | 1400 | 500 | 2 | Yes | 70 | 140 | 60 |
| Enterprise | 2000 | 1000 | 3 | Yes | 999 | Unlimited | Unlimited |

**Budget Enforcement**:
- Budget checks happen during pre-flight before any processing begins
- Quota is tracked per-workspace, per-tier
- Exceeding any budget limit raises `SchemaViolationError` immediately

---

## Pipeline Components

### 1. Document Ingestion

**Module**: `bounding.py`  
**Class**: `DocumentIngestor`

Converts raw document bytes into chunked `Chunk` objects with canonicalized text and origin tracking:

```python
class DocumentIngestor:
    def ingest(self, req: ExtractionRequest, raw_bytes: bytes) -> DocumentBuffer:
        # Splits document into chunks respecting tier max_chunk_chars
        # Preserves origin_offset for coordinate verification
        # Computes chunk hashes for lineage tracking
        pass
```

**Chunk Properties**:
- `chunk_id`: Unique identifier per chunk
- `text`: Canonicalized chunk text
- `origin_offset`: Original position in source document
- `hash16`: 16-character hash of chunk content

---

### 2. ExtractorAgent

**Module**: `agents.py`  
**Timeout**: Configurable, default 60s

The ExtractorAgent extracts candidate claims from document chunks based on the request query and target fields:

```python
class ExtractorAgent:
    async def extract(
        self,
        query: str,
        target_fields: List[str],
        chunks: List[Chunk],
        workspace: str,
        tier: str,
        correction_hint: Optional[str] = None,
    ) -> List[Claim]:
        # Extracts claims matching target fields from chunks
        # Retries up to budget.retries times on schema violation
        pass
```

**Key Characteristics**:
- Retry count limited by tier budget
- Correction hints passed on schema violation
- LLM usage only allowed for Pro+ tiers
- Circuit breaker protected

---

### 3. SkepticAgent

**Module**: `agents.py`

The SkepticAgent performs cross-examination of each extracted claim against its source chunk:

```python
class SkepticAgent:
    def cross_examine(
        self, claim: Claim, chunk_map: Dict[str, Chunk], doc_stub: DocumentBuffer, query: str
    ) -> Verification:
        # Performs:
        # 1. Coordinate bounds check
        # 2. Negation window analysis
        # 3. Contradiction detection
        # 4. Shape matching
        pass
```

**Verification Checks**:

1. **Coordinate Bounds Check**: Verifies `char_start` and `char_end` are within chunk bounds
2. **Negation Window Analysis**: Scans window around claim for negation words
3. **Contradiction Detection**: Compares claim value against source text
4. **Shape Matching**: Validates claim matches requested value shape (if provided)

**Rejection Reason Codes**:
- `REASON_NEGATED`: Claim appears negated in source text
- `REASON_OUT_OF_BOUNDS`: Coordinates outside chunk bounds
- `REASON_CONTRADICTION`: Claim contradicts source text
- `REASON_SHAPE_MISMATCH`: Value does not match requested shape

---

### 4. AdjudicatorAgent

**Module**: `agents.py`  
**Strict Mode**: Configurable, default True

The AdjudicatorAgent takes verified claims from the SkepticAgent and produces the final `VerifiedPayload`:

```python
class AdjudicatorAgent:
    def adjudicate(
        self, document_id: str, claims: List[Claim], chunks_by_id: Dict[str, Chunk], verdicts: List[Verification]
    ) -> VerifiedPayload:
        # Builds lineage index
        # Computes conformance score
        # Constructs Merkle root for audit trail
        pass
```

**Adjudication Output**:
- `conformance_score`: 0-100 score of claim-source alignment
- `merkle_root`: Root hash of claim evidence tree
- `audit_hash`: Cryptographic audit trail hash
- `rejection_summary`: Count of claims rejected per reason code

---

## Verification Primitives

### 1. Negation Window Analysis

**Module**: `_verify.py`

Scans a configurable window around each claim for negation words:

```python
def negation_window(text: str, start: int, end: int) -> Tuple[int, int, bool]:
    # Returns (window_start, window_end, has_negation)
    # Window extends ±N characters around claim coordinates
    pass
```

### 2. Contradiction Detection

**Module**: `_verify.py`

Compares claim value against source text using canonical string comparison:

```python
def contradiction(claim_value: str, source_text: str) -> bool:
    # Returns True if claim contradicts source
    # Uses canonicalization to normalize comparison
    pass
```

### 3. Canonicalization

**Module**: `bounding.py`

Normalizes text for comparison by:
- Removing extra whitespace
- Lowercasing (configurable)
- Normalizing punctuation
- Stripping diacritics

```python
def canonicalize(text: str) -> Tuple[str, Dict]:
    # Returns (canonical_text, metadata)
    # Metadata tracks transformations applied
    pass
```

### 4. LineageIndex

**Module**: `_verify.py`

Tracks the provenance of each verified claim:

```python
class LineageIndex:
    def record(self, field: str, value: str, document_id: str, chunk_id: str, start: int, end: int) -> None:
        # Records exact coordinate where claim was found
        # Builds evidence chain for audit
        pass
```

### 5. Merkle Root Construction

**Module**: `_runtime.py`

Builds a Merkle tree of verified claims for cryptographic audit trails:

```python
def merkle_root(items: List[bytes]) -> str:
    # Computes deterministic Merkle root hash
    # Same input always produces same root
    pass
```

---

## Operation Modes

### 1. Full-Run Non-Streaming

```python
async def run(self, req: ExtractionRequest, raw_bytes: bytes) -> VerifiedPayload:
    # Complete pipeline in one call
    # Returns all verified claims at once
    # Logs elapsed time and conformance score
    pass
```

**Flow**:
1. Pre-flight budget check
2. Quota consumption
3. Document ingestion
4. Extraction (with retries)
5. Skeptic cross-examination
6. Adjudication
7. Post-build coordinate verification
8. Return `VerifiedPayload`

### 2. Streaming Batch Mode

```python
async def run_streaming(self, req: ExtractionRequest, raw_bytes: bytes) -> VerifiedPayload:
    # Processes chunks in batches of chunk_batch_size (default 25)
    # Claims verified incrementally
    # Returns complete payload at end
    pass
```

**Batch Processing**:
- Chunks processed in batches of configurable size
- Claims verified as batches complete
- Duplicate claims merged with evidence count incremented
- Post-build coordinate verification still runs on final payload

### 3. Async Streaming Iterator

```python
async def run_streaming_iter(self, req: ExtractionRequest, raw_bytes: bytes) -> AsyncIterator[VerifiedClaim]:
    # Yields individual VerifiedClaim objects as they are verified
    # Caller can process claims incrementally
    # No final payload returned
    pass
```

**Iterator Characteristics**:
- Yields claims immediately after verification
- No duplicate merging
- No post-build verification
- Caller receives claims one at a time

---

## Runtime Protection

### 1. QuotaTracker

**Module**: `_runtime.py`

Tracks monthly query and MB consumption per workspace:

```python
class QuotaTracker:
    def check_and_consume(self, workspace: str, tier: str, mb_used: float, max_queries: int, max_mb: int) -> None:
        # Raises error if quota exceeded
        # Consumes quota on successful check
        pass
```

### 2. CircuitBreaker

**Module**: `_runtime.py`

Per-AHE circuit breaker for external dependencies:

```python
class CircuitBreaker:
    def state(self) -> CircuitState:
        # Returns: CLOSED, OPEN, HALF_OPEN
        pass
```

### 3. TokenCaps

**Module**: `_runtime.py`

Enforces token/character limits per field and globally:

```python
class TokenCaps:
    def check_field(self, field_name: str, value: str) -> None:
        # Raises error if field exceeds per-field cap (1024 chars)
        pass
    
    def check_total(self, total_chars: int) -> None:
        # Raises error if total exceeds global cap
        pass
```

### 4. Pre-Flight Projection

Before any processing begins, AHE projects memory requirements:

```python
def project_budget(max_chunks: int, max_claims: int, avg_chunk_chars: int) -> Dict:
    # Projects total memory required
    # Raises error if exceeds max_memory_bytes
    pass
```

---

## Input Validation

### 1. HTML Tag Balancing

For HTML documents, verifies all tags are properly balanced before ingestion:

```python
def balance_tags(raw_bytes: bytes) -> Tuple[bool, str]:
    # Returns (is_balanced, error_message)
    pass
```

### 2. Code Syntax Validation

For Python/JavaScript documents, checks for undefined names and cross-language leaks:

```python
def py_undefined_names(code: str) -> Set[str]:
    # Returns set of undefined names in Python code
    pass


def cross_language_leak(code: str) -> bool:
    # Returns True if code contains cross-language injection
    pass
```

### 3. JSON FSA Validation

Validates JSON structure using finite-state automaton:

```python
def json_fsa_valid(json_bytes: bytes) -> bool:
    # Returns True if JSON is syntactically valid
    pass
```

---

## Output Schema

### VerifiedPayload

```python
@dataclass
class VerifiedPayload:
    document_id: str
    claims: List[VerifiedClaim]
    conformance_score: int
    audit_hash: str
    merkle_root: str
    chunk_hashes: Optional[Dict[str, str]]
    rejection_summary: Dict[str, int]
```

### VerifiedClaim

```python
@dataclass
class VerifiedClaim:
    field_name: str
    value: str
    confidence: float
    coordinate: ClaimCoordinate
    evidence_count: int
    verification: Verification
```

### ClaimCoordinate

```python
@dataclass
class ClaimCoordinate:
    document_id: str
    chunk_id: str
    char_start: int
    char_end: int
```

---

## Self-Diagnostic

The AHE includes a comprehensive self-diagnostic method:

```python
def self_diagnose(self) -> dict:
    report = {
        "engine_version": "axelr-ahe/1.7",
        "adjudicator_strict": bool,
        "chunk_batch_size": int,
        "emit_chunk_hashes": bool,
        "signed": bool,
        "tiers": List[str],
        "circuit_breaker": dict,
        "mph_ok": bool,
        "canon_ok": bool,
        "merkle_ok": bool,
        "healthy": bool,
    }
    return report
```

**Diagnostic Checks**:
1. MinimalPerfectHash functionality
2. Canonicalization correctness
3. Merkle root determinism
4. Circuit breaker state
5. All tier configurations present

---

## Configuration Reference

```python
class AntiHallucinationEngine:
    def __init__(
        self,
        extractor_timeout_s: float = 60.0,
        default_max_chunk_chars: int = 1400,
        adjudicator_strict: bool = True,
        quota: Optional[QuotaTracker] = None,
        chunk_batch_size: int = 25,
        emit_chunk_hashes: bool = False,
        signing_secret: Optional[bytes] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ):
        pass
```

---

## Performance Characteristics

| Operation | Typical Latency | Notes |
|-----------|-----------------|-------|
| Document Ingestion | O(n) linear | Depends on document size |
| Extraction | Variable | Depends on chunk count, tier |
| Skeptic Cross-Examination | <1ms per claim | Pure deterministic checks |
| Adjudication | <1ms total | Lineage indexing + scoring |
| Post-Build Verification | O(claims) | Coordinate bounds + canonicalization |

**Total Pipeline Latency**:
- Free tier: ~1-5s (no LLM)
- Pro tier: ~5-15s (with LLM, 1 retry max)
- Enterprise tier: ~10-30s (with LLM, 3 retries max)

---

## Implementation Notes

1. **No Fake Claims**: The AHE is a deterministic verification system, not an AI model. It does not "detect hallucinations" — it verifies claims are directly derivable from source text using explicit coordinate matching.

2. **Strict Post-Build Verification**: After payload construction, every claim coordinate is re-verified against source text. Any mismatch raises `SchemaViolationError`.

3. **Deterministic Output**: Same input document + same query always produces identical output. No randomness in verification logic.

4. **Budget First**: All budget and quota checks happen before any processing begins. No partial resource consumption on failure.

5. **Audit Trail**: Every payload includes a Merkle root and audit hash that can be used to prove claim provenance.

6. **v1.7 Fix**: The v1.7 release fixed a trailing v1.0 duplicate class definition that caused a module-level syntax error in v1.5. All v1.6 dispatch/health/batch features are preserved.

---

## Error Hierarchy

| Exception | Meaning |
|-----------|---------|
| `DocumentParseError` | Document format or size invalid |
| `SchemaViolationError` | Claim does not match expected schema or shape |
| `UnverifiableDataError` | No verifiable claims could be extracted |
| `UnverifiedClaimError` | Individual claim failed verification |