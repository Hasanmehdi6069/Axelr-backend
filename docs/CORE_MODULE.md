# AXELR Core Module Documentation

## Overview
The AXELR Core module (`core/`) is the foundational intelligence layer of the AXELR AI system, providing a comprehensive, layered architecture for AI inference, resilience, memory, code intelligence, and anti-hallucination guarantees. It's organized in a 12-layer architecture with strict dependency ordering and zero circular imports.

**Version**: 6.0.0  
**Status**: Production-ready  
**Runtime**: Async Python 3.10+  
**Dependency Rule**: Lower layers never import from higher layers

---

## Layered Architecture Overview

The core module follows a strict layered architecture where each layer only depends on layers below it:

```
Layer 11: Cognitive Pipeline & Reflection
Layer 10: Consensus & Adaptive
Layer 9: Goat Layer & Hardening
Layer 8: Condenser & Context
Layer 7: Memory & Repo Indexer
Layer 6: Cache & Persistence
Layer 5: Guards & Shedder
Layer 4: Resilience & Observe
Layer 3: Foundation & Compliance
```

---

## Layer 3: Foundation & Compliance Layer

This is the bottom layer - no imports from any other core modules.

### `foundation.py` - Root Configuration & Error Hierarchy
The absolute root of the core DAG. All other core modules import from here.

**Key Components**:
- **`Settings`**: Frozen typed configuration class with environment variable overrides
  - 60+ configurable parameters
  - All values type-checked at load time
  - Cached singleton pattern
  - Audit method returns full configuration dictionary
- **Exception Hierarchy**: All exceptions inherit from `AxelrError`
  - `ProviderError`: AI provider failures
  - `CircuitOpen`: Circuit breaker open
  - `RateLimited`: Rate limit exceeded
  - `QuotaExceeded`: Token quota exceeded
  - `TimeoutError`: Request deadline exceeded
  - `ValidationError`: Input validation failures
- **`with_retry()`**: Canonical async retry primitive
  - 3 predefined retry policies
  - Exponential backoff with jitter
  - Per-operation timeout configuration
  - Only retry primitive in the entire codebase

**Retry Policies**:
| Policy | Max Retries | Backoff | Use Case |
|--------|-------------|---------|----------|
| `POLICY_DEFAULT` | 3 | 1s, 2s, 4s | General operations |
| `POLICY_NETWORK` | 5 | 1s, 2s, 4s, 8s, 16s | Network requests |
| `POLICY_IDEMPOTENT` | 7 | Aggressive | Idempotent operations |

### `compliance.py` - PII & Language Detection
Static compliance checks that run before any request processing.

**Key Features**:
- **PII Detection**: Detects and redacts:
  - Email addresses
  - Phone numbers (international format)
  - Credit card numbers (Luhn validated)
  - Social security numbers
  - IP addresses
- **Language Detection**: Fast language detection with confidence scores
  - Supports 100+ languages
  - Confidence threshold > 0.8 required
  - RTL language detection and handling
- **Sanitization**: Automatic PII redaction with replacement tokens

---

## Layer 4: Resilience & Observe Layer

System resilience and observability infrastructure.

### `resilience.py` - Circuit Breakers & Failover
Production-grade resilience patterns.

**Key Components**:
- **`CircuitBreaker`**: Per-provider circuit breaker
  - Configurable failure threshold (default: 3)
  - Cooldown period (default: 60s)
  - Automatic half-open recovery
  - State tracking: CLOSED → OPEN → HALF-OPEN → CLOSED
- **`ladder_run()`**: Tiered failover execution
  - Runs providers in priority order
  - Stops at first successful result
  - Records failure patterns
- **`HotSwapDetector`**: Detects and handles provider hot swaps
  - Monitors provider response patterns
  - Triggers re-ranking on performance changes
- **`WaveRacer`**: First-token race detection
  - Runs multiple providers in parallel
  - Returns first successful result
  - Tracks latency distributions

### `observe.py` - Telemetry & Tracing
Comprehensive observability infrastructure.

**Key Components**:
- **`TraceRing`**: Circular buffer for request tracing
  - Stores last 1000 request traces
  - Each trace includes: timing, provider, status, errors
  - Fixed memory footprint (~1MB)
- **`Meter`**: Performance metering and statistics
  - Per-provider latency tracking
  - Success rate calculation
  - Token usage statistics
  - Rolling window averages
- **`trace_scope()`**: Context manager for tracing
  - Automatically records timing
  - Captures exceptions
  - Adds metadata to traces
- **Memory Tuning**: Automatic memory monitoring and trimming
  - Tracks RSS usage
  - Trims Python objects under memory pressure
  - Configurable thresholds

---

## Layer 5: Guards & Shedder Layer

System protection and load management.

### `guards.py` - Thermal & Token Protection
Runtime protection guards that run on every request.

**Key Guards**:
- **Thermal Guard**: CPU/GPU temperature monitoring and throttling
  - Warm threshold: 65% → warning
  - Hot threshold: 80% → reduce parallelism
  - Critical threshold: 92% → circuit break
  - Configurable temperature polling interval
- **Micro-Volatile Guard**: Memory usage protection
  - Limits request context size to 800 chars
  - Maximum 128 tokens for volatile requests
  - 15MB memory cap per request
- **Token Governor**: Dynamic token reservation and settling
  - Per-tier token limits (Free: 300k/day, Pro: 2M/day, Elite: 8M/day)
  - Dynamic token settling based on load
  - Real-time quota tracking
- **Burst Scheduler**: Controlled burst execution
  - Maximum 4 active burst requests
  - Burst queue with priority ordering
  - Configurable burst window (10-20 minutes)

### `shedder.py` - Complexity Assessment & Load Shedding
Intelligent load shedding based on request complexity.

**Key Functions**:
- **`assess_complexity()`**: Assess request complexity score (0-10)
  - Analyzes prompt length
  - Counts code blocks
  - Estimates reasoning required
  - Detects multi-step tasks
- **`shedder_should_shed()`**: Determine if request should be shed
  - Based on system load
  - Complexity score
  - User tier priority
- **Adaptive Budget Management**: Dynamic token budget allocation
  - Adjusts based on system health
  - Prioritizes higher tier users
  - Maintains minimum throughput

### `infra.py` - Infrastructure Utilities
Low-level infrastructure utilities.

**Key Features**:
- **Redis Client**: Async Redis client with connection pooling
  - Semantic cache operations
  - Rate limit tracking
  - Persistent state storage
- **Context Registry**: Global context management
  - Tracks active requests
  - Context cleanup on completion
- **Cloudflare Embedding Batch Processing**: Batch embedding operations
  - Up to 100 embeddings per batch
  - Rate limit aware batching

---

## Layer 6: Cache & Persistence Layer

Caching and persistence infrastructure.

### `cache.py` - Multi-Layer Caching System
Two-level semantic caching system.

**Cache Hierarchy**:
1. **Cloudflare Vectorize**: Remote semantic cache
   - 40-70% hit rate on production
   - Vector similarity search
   - Global edge distribution
2. **Local Semantic Cache**: In-memory BM25 index
   - Up to 5000 documents
   - TTL: 1 hour default
   - Fast in-memory lookup

**Key Features**:
- Provider cache hints and hit/miss tracking
- Cache invalidation on configuration changes
- Semantic similarity threshold: 0.85

### `persistence.py` - Conversation & User Profile Persistence
Async persistence layer for conversation history.

**Key Components**:
- **`PersistenceEngine`**: Async persistence layer
  - Supabase backend
  - Conversation history storage
  - User profile management
- **`UserProfile`**: User profile management
  - Preferences storage
  - Usage statistics
  - Tier configuration
- **`ChatMessage`**: Chat message storage
  - Full message history
  - Token usage tracking
  - Timestamp metadata

---

## Layer 7: Memory & Repo Indexer Layer

Long-term memory and code repository analysis.

### `memory.py` - Conversation Memory & Vector Storage
Long-term memory management system.

**Key Components**:
- **`Memory`**: Long-term memory management
  - Vector storage of past conversations
  - Semantic search over memory
  - Automatic memory pruning
- **`ConversationMemory`**: Conversation-specific memory
  - Per-conversation context tracking
  - History condensation
  - Context window management
- **`MemoryBus`**: Memory event bus
  - Memory change notifications
  - Cross-module memory coordination

### `repo_indexer.py` - Code Repository Indexing & Search
Code repository analysis and semantic search.

**Key Features**:
- **`RepoIndexer`**: Index code repositories
  - Recursive file traversal
  - Code chunking (100-500 lines per chunk)
  - Syntax-aware chunk boundaries
- **`CodeChunk`**: Code chunk representation
  - File path, line range
  - Language detection
  - Semantic embedding
- **Semantic Code Search**: Natural language code search
  - BM25 + semantic ranking
  - Code-aware similarity scoring
  - Fast sub-second queries

### `context.py` - Intent Classification & Context Management
Context building and intent classification.

**Key Functions**:
- **`classify_intent()`**: Intent classification
  - Task type detection
  - Complexity estimation
  - Tool requirement detection
- **`clip_history()`**: History clipping for context window management
  - Preserves most relevant context
  - Maintains conversation flow
  - Semantic anchor preservation
- **Vault Building**: Prompt enhancement vault construction
  - Relevant memory injection
  - Repository context injection
  - User preference injection

---

## Layer 8: Condenser & Context Layer

History compression and context anchoring.

### `condenser.py` - History Compression & Anchoring
Conversation history compression system.

**Key Functions**:
- **`condense()`**: Condense conversation history
  - 2:1 compression ratio while preserving context
  - Semantic anchor preservation
  - Critical information retention
- **`compress_history()`**: Compress history with configurable ratio
  - Lossless compression for small histories
  - Lossy compression for large histories
- **Anchor Detection**: Semantic anchor detection and management
  - Identifies critical conversation points
  - Preserves anchors through compression
  - Uses anchors for context retrieval

### `hardening.py` - System Hardening & AST Repair
System hardening and automatic code repair.

**Key Features**:
- **`ast_repair()`**: Automatic AST repair
  - Fixes syntax errors in generated code
  - Validates imports
  - Type checks generated code
- **`race_tasks()`**: Task racing for improved reliability
  - Runs multiple independent tasks in parallel
  - Returns first successful result
  - Tracks task performance
- **Provider Event Telemetry Ring**: Per-provider event ring buffer
  - Last 100 events per provider
  - Failure pattern analysis
  - Performance trend tracking

---

## Layer 9: Goat Layer & Hardening

Cognitive pipeline and reflection system.

### `goat_layer.py` - Cognitive Pipeline & Reflection
Main cognitive execution pipeline.

**Key Functions**:
- **`run_cognitive_pipeline()`**: Execute cognitive pipeline
  - Multi-stage reasoning process
  - Reflection and self-correction
  - Quality gates at each stage
- **`llm_reflect()`**: LLM reflection and self-repair
  - Reviews generated responses
  - Identifies hallucinations
  - Proposes corrections
- **Workload Classification**: Automatic workload classification
  - Code vs reasoning vs creative
  - Optimal provider selection
  - Parallelism configuration

---

## Layer 10: Consensus & Adaptive Layer

Multi-provider consensus and adaptive parameters.

### `consensus.py` - Multi-Provider Consensus & Polishing
Multi-provider consensus system.

**Key Features**:
- **`race_consensus()`**: Race multiple providers for consensus
  - Runs 2-3 providers in parallel
  - Compares results for agreement
  - Returns highest confidence result
- **`local_polish()`**: Local response polishing
  - Post-processing of LLM outputs
  - Formatting and consistency checks
  - Grammar and spelling correction
- **Stage-Based Cognitive Execution**: Multi-stage execution
  - Each stage has its own quality gate
  - Early termination on quality failure
  - Stage-specific provider selection

### `adaptive.py` - Adaptive Model Parameters
Dynamic parameter tuning based on request characteristics.

**Key Functions**:
- **`tune_temperature()`**: Dynamic temperature tuning
  - Lower temperature for factual tasks
  - Higher temperature for creative tasks
  - Based on complexity classification
- **`StreamQualityGate`**: Streaming quality control
  - Real-time streaming validation
  - Early quality warnings
  - Automatic stream correction

---

## Layer 11: Cognitive Pipeline & Reflection

Highest layer - full cognitive execution.

### `pipeline.py` - Main Execution Pipeline
End-to-end request processing pipeline.

**Pipeline Stages**:
1. **Input Validation**: Compliance and PII checks
2. **Complexity Assessment**: Shedder complexity score
3. **Context Building**: Memory and repository context
4. **Provider Selection**: AI router decision
5. **Execution**: Provider request execution
6. **Verification**: Anti-hallucination checks
7. **Response Generation**: Final response formatting
8. **Post-Processing**: Consensus and polishing

---

## Core Subsystems

### Anti-Hallucination Engine (`anti_hallucination/`)
Standalone anti-hallucination verification system. See separate documentation in `AHE_ENGINE.md`.

### Sovereign Engine (`axelr-sovereign.engine/`)
Local inference and runtime engine. See separate documentation in `SOVEREIGN_ENGINE.md`.

---

## Configuration Reference

All core configuration is in `Settings` class:

| Category | Key Parameters |
|----------|----------------|
| **Pipeline** | `deadline_s`, `max_extra_calls`, `cognition_level` |
| **Thermal** | `thermal_warm_pct`, `thermal_hot_pct`, `thermal_critical_pct` |
| **Circuit Breaker** | `cb_threshold`, `cb_cooldown_s` |
| **Twin Engine** | `twin_max_prompt_chars`, `twin_fast_parallel` |
| **Token Governor** | `tok_free_min`, `tok_pro_min`, `tok_biz_min` |
| **Memory** | `memory_threshold_mb`, `memory_panic_mb` |

---

## Performance Characteristics

- **Baseline Memory**: ~150MB core footprint
- **Cold Start**: ~2s initialization time
- **Request Overhead**: ~50ms before AI provider call
- **Cache Hit Rate**: 40-70% with semantic caching
- **Circuit Breaker Recovery**: Automatic within 60s

---

## Testing

All core modules have comprehensive test coverage:
```bash
pytest core/tests/ -v --cov=core
```

Test coverage targets: 85%+ for all core modules.