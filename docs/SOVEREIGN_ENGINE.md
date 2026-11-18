# AXELR Sovereign Engine

**Version**: v1.0  
**Module**: `core/axelr-sovereign.engine/`  
**Memory Footprint**: ~4 MB runtime + configurable KV cache  
**Status**: Production-ready, verified implementation

---

## Overview

The Sovereign Engine is AXELR's local inference runtime layer that operates entirely without external API calls when configured. It provides deterministic, low-latency text processing through a collection of specialized algorithms, phonetic matching, and context injection systems. This is not a full LLM inference engine — it is a lightweight, rule-based and statistical runtime that handles the majority of common text correction, normalization, and context injection tasks locally before any cloud fallback is considered.

---

## Core Architecture Components

### 1. StaticGraphRuntime (SGO #7)

**File**: `axelr_engine.py`  
**Memory**: Configurable arena allocation

The StaticGraphRuntime reserves a contiguous float32 memory arena for tensor operations. This is honest pre-allocation, not a promise: the arena is real numpy memory the decoder can slice into. Real GGML/ONNX would pass the arena pointer into their context init.

```python
class StaticGraphRuntime:
    def reserve(self, profile):
        # Reserves (max_memory_mb - 20) MB as float32 arena
        # 20 MB overhead reserved for SGO structures
        pass
    
    def commit(self) -> bool:
        # Actually allocates the arena on first inference
        # Returns False if memory allocation fails
        pass
```

**Key Characteristics**:
- Contiguous float32 arena allocation
- Memory reservation happens during profile loading
- Commit only occurs on first real inference
- Graceful fallback if arena allocation fails
- 20 MB fixed overhead for SGO structures

---

### 2. ConfidenceCalibrator (#16)

**File**: `axelr_engine.py`  
**Algorithm**: Temperature-scaling sigmoid calibration

The ConfidenceCalibrator converts raw logit values into calibrated confidence scores using temperature scaling. It adapts its temperature parameter over time based on observed correctness.

```python
class ConfidenceCalibrator:
    def calibrate(self, raw_logit: float) -> float:
        # sigmoid(logit / temperature)
        # Range: 0.0 - 1.0
        pass
    
    def observe(self, raw_logit: float, was_correct: bool) -> None:
        # Adaptive temperature adjustment
        # +0.005 temp when high confidence was wrong
        # -0.005 temp when low confidence was right
        pass
```

**Calibration Configuration**:
- Initial temperature: Configurable per profile
- Min temperature: Lower bound to prevent over-correction
- Max temperature: Upper bound to prevent under-correction
- Learning rate: 0.005 per observation

---

### 3. Phonetic Algorithms (PEDA #6)

**File**: `axelr_engine.py`  
**Memory**: ~2 MB for phonetic indices

The Sovereign Engine implements three phonetic algorithms for fuzzy matching and correction:

#### 3.1 Soundex

Standard Soundex algorithm that converts words into phonetic codes based on their pronunciation. Single source implementation designed to be portable to JavaScript for client-side validation.

```python
def soundex(word: str) -> str:
    # Returns 4-character Soundex code
    # First character preserved, rest mapped to phonetic groups
    # Padded with zeros if needed
    pass
```

#### 3.2 Double Metaphone

Core Double Metaphone algorithm with silent leading combinations handling:
- `kn`, `gn` → silent leading consonant
- `ps` → silent 'p'
- `rh` → silent 'h'
- `wh` → silent 'w'
- `wr` → silent 'w'

```python
def metaphone(word: str) -> str:
    # Returns 4-character Metaphone code
    # Handles complex phonetic transformations
    # More accurate than Soundex for modern English
    pass
```

#### 3.3 PhonemeGraphemeNormalizer (LPGN #15)

Normalizes phonetic representations into grapheme equivalents using a fixed mapping table:

| Phoneme | Grapheme |
|---------|----------|
| ah      | a        |
| eh      | e        |
| ih      | i        |
| oh      | o        |
| uh      | u        |
| ck      | c        |
| ks      | x        |
| kw      | qu       |
| shun    | tion     |

#### 3.4 PhoneticEnsemble

Combines Soundex and Metaphone into a unified similarity scoring system:

```python
class PhoneticEnsemble:
    @staticmethod
    def compare(a: str, b: str) -> float:
        # Returns 0.0-1.0 similarity score
        # 0.5 weight from Soundex match
        # 0.5 weight from Metaphone match
        pass
```

---

### 4. TrieLexicon (TFLP #13)

**File**: `axelr_engine.py`  
**Memory**: ~1.5 MB for typical lexicon

A prefix tree (trie) implementation for fast dictionary lookups and prefix suggestions.

```python
class TrieLexicon:
    def insert(self, word: str) -> None:
        # Insert word into trie structure
        pass
    
    def contains(self, word: str) -> bool:
        # Exact match lookup
        pass
    
    def prefix(self, prefix: str, limit: int = 5) -> List[str]:
        # Return top N words matching prefix
        pass
```

---

### 5. PhoneticCorrector

**File**: `axelr_engine.py`  
**Algorithm**: Ensemble scoring across phonetic indices

Combines all phonetic algorithms and the trie lexicon to provide spelling correction candidates:

1. LPGN normalization short-circuit check
2. Gather candidates from Soundex index
3. Gather candidates from Metaphone index
4. Gather candidates from trie prefix matches
5. Rank candidates using ensemble scoring
6. Return highest-scoring correction

**Common Words Bias**: A fixed set of commonly confused words receives a 0.5 scoring bias to prioritize correct homophone usage.

---

### 6. ZeroLatencyContextInjector (ZLCI #3)

**File**: `axelr_features.py`  
**Latency**: <1ms extraction, zero additional inference cost

The ZLCI system extracts domain context from window title and active text without requiring any model inference. It uses pre-compiled regex patterns to identify domain hints and inject biasing terms into the decoding process.

```python
class ZeroLatencyContextInjector:
    def update(self, data: Dict) -> ContextSnapshot:
        # Extract domain hints from window context
        # Cached by timestamp to avoid redundant processing
        pass
    
    def biasing_terms(self) -> Tuple[Set[str], Set[str], float]:
        # Returns:
        # - Keywords to boost
        # - Common words set
        # - Boost multiplier (1.0 - domain-specific max)
        pass
```

**Domain Configuration**: Each domain has:
- Regex patterns for detection
- Biasing keywords specific to that domain
- Boost multiplier for domain-relevant terms

**Cached Context**: Results are cached by timestamp, so repeated calls with the same context return immediately.

---

### 7. EvictingKVCache (#9)

**File**: `axelr_features.py`  
**Algorithm**: LRU eviction with hard byte cap

An LRU cache implementation for storing key-value tensor data with strict memory limits:

```python
class EvictingKVCache:
    def __init__(self, max_memory_mb: float):
        # Cache size is strictly bounded by max_memory_mb
        # No soft limits, eviction happens immediately when cap exceeded
        pass
    
    def put(self, key: str, data: np.ndarray, ttl: float = 300.0) -> None:
        # Evicts least recently used entries if cap exceeded
        pass
    
    def get(self, key: str) -> Optional[np.ndarray]:
        # Moves entry to end of LRU order on access
        pass
```

**Key Characteristics**:
- OrderedDict-based LRU implementation
- Hard byte cap, no over-allocation
- Entries have TTL metadata (not enforced by default)
- Eviction is synchronous and deterministic
- Memory overhead: ~300 KB bookkeeping

---

### 8. StreamingLogitBiasing (SCLB #14)

**File**: `axelr_features.py`  
**Cooldown**: 0.1s minimum between updates

Applies context-based biasing to logits during streaming inference with a cooldown period to prevent thrashing:

```python
class StreamingLogitBiasing:
    def bias_terms(self, now: float) -> Tuple[Set[str], float]:
        # Returns empty set if cooldown not elapsed
        # Otherwise returns ZLCI biasing terms
        pass
```

---

### 9. TokenRescoringLoop (#10)

**File**: `axelr_features.py`  
**Queue Size**: 64 entries max

A background async queue that applies deterministic n-gram grammar repairs:

| Pattern | Correction |
|---------|------------|
| your welcome | you're welcome |
| would of | would have |
| could of | could have |
| it's own | its own |

**Key Characteristics**:
- Runs in separate async task
- Queue drops entries if full (warning logged)
- Only applies deterministic, unambiguous repairs
- No language model used in this loop

---

### 10. StreamingNGramReranker (#20)

**File**: `axelr_features.py`  
**Beam Width**: 4, Window Size: 5

Applies curated phrase→phrase substitutions to catch homophone pairs that edit-distance scoring cannot distinguish.

**Important**: This is NOT a language model reranker — it applies deterministic phrase substitutions from a fixed repair table. Documented as such so no caller mistakes it for an actual LM.

**Repair Sources**:
- Homophone context table from `runtime-tables.json`
- Context-aware homophone corrections
- Applied only to trailing N tokens of the stream

---

### 11. StructuredRepair (#21)

**File**: `axelr_features.py`  
**Memory**: ~12 KB

High-confidence pattern repairs for structured data formats. Only rewrites when pattern is unambiguous.

**Supported Format Conversions**:

| Pattern Type | Input Format | Output Format |
|--------------|--------------|----------------|
| Percent | "X percent" | "X%" |
| Dollars | "X dollars" | "$X" |
| Euros | "X euros" | "€X" |
| Pounds | "X pounds" | "£X" |
| Time | "HH MM am/pm" | "HH:MM am/pm" |
| Date | "Month DD, YYYY" | "MM/DD/YYYY" |
| Email | "user at domain dot com" | "u***@***********" |
| Phone | "XXX XXX XXXX" | "(XXX) ***-****" |
| URL | "www dot domain dot com" | "www.domain.com" |
| Units | "X meters/kg/seconds" | "Xm/Xkg/Xs" |

**Number Word Mapping**: Converts word numbers to digits (zero→0, one→1, ..., hundred→100)

---

### 12. CloudRouter

**File**: `axelr_features.py`  
**Timeout**: 0.5s default timeout

Wraps a CircuitBreaker for the decoder's cloud fallback:

```python
class CloudRouter:
    @property
    def enabled(self) -> bool:
        # Returns True only if both URL and API key are configured
        pass
    
    @property
    def breaker(self) -> CircuitBreaker:
        # Per-cloud-provider circuit breaker
        pass
```

---

## Configuration Reference

### DecoderConfig

```python
@dataclass(frozen=True, slots=True)
class DecoderConfig:
    confidence_threshold: float = 0.7
    cloud_fallback_url: Optional[str] = None
    api_key: Optional[str] = None
    max_local_memory_mb: int = 150
    model_quantization: str = "int4"
```

### HardwareProfile

Configurable hardware profile that determines memory allocation and quantization settings.

### SamplingConfig

Temperature, top-k, top-p sampling parameters for local inference.

### CalibrationConfig

```python
@dataclass
class CalibrationConfig:
    initial_temperature: float = 1.0
    min_temperature: float = 0.5
    max_temperature: float = 2.0
```

---

## Performance Characteristics

| Component | Memory | Latency | Deterministic |
|-----------|--------|---------|---------------|
| Phonetic Ensemble | ~2 MB | <1ms | Yes |
| Trie Lexicon | ~1.5 MB | <0.1ms | Yes |
| ZLCI | negligible | <1ms | Yes |
| KV Cache | configurable | O(1) | Yes |
| Confidence Calibrator | negligible | <0.1ms | Yes |
| Structured Repair | ~12 KB | <1ms | Yes |
| StaticGraphRuntime | ~20 MB overhead | allocation only | N/A |

**Total Runtime Memory**: ~4 MB fixed + configurable KV cache

---

## Implementation Notes

1. **No Fake Claims**: The Sovereign Engine is a lightweight local runtime, not a full LLM. It handles deterministic corrections and context injection; cloud fallback handles complex inference.

2. **Portable Phonetics**: All phonetic algorithms are implemented to be portable to JavaScript for client-side validation.

3. **Deterministic Operations**: All core operations are deterministic given the same input. No randomness in correction logic.

4. **Conservative Repairs**: Structured repair only applies unambiguous patterns. No speculative corrections.

5. **Circuit Breakers**: Cloud fallback has its own circuit breaker with 5-failure threshold and 30s recovery.

---

## Runtime Tables

All shared runtime data is loaded from `runtime-tables.json` as a single source of truth:
- Homophone word list
- Homophone context mapping
- Domain patterns and configurations
- Common words set

---

## Self-Diagnostic Checks

The Sovereign Engine includes no built-in self-diagnostic, but all components expose their state through standard Python properties.