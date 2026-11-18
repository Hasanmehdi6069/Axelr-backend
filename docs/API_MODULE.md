# AXELR API Module Documentation

## Overview
The AXELR API module (`api/`) is the public-facing interface layer for the AXELR AI backend, providing a FastAPI-based REST and WebSocket API with comprehensive lifecycle management, rate limiting, middleware, and workspace orchestration capabilities. This is the single entry point for all external interactions with the AXELR system.

**Version**: 27.0  
**Status**: Production-ready  
**Runtime**: Async Python 3.10+ with Uvicorn

---

## Core Architecture

The API module is organized in a layered architecture with clear separation of concerns:

```
api/
├── lifecycle.py          # Application boot, health, shutdown
├── limits.py             # Tiered rate limiting, quota management
├── middleware.py         # Request processing pipeline
├── state.py              # Global application state
├── ai_router.py          # Intelligent AI provider routing
├── workspace_engine.py   # Workspace detection, code analysis
├── routes_chat.py        # Chat completion endpoints
├── routes_admin.py       # Admin management endpoints
├── routes_ops.py         # Operations monitoring endpoints
├── routes_tools.py       # Tool execution endpoints
└── relay/                # WebSocket relay infrastructure
```

---

## Core Components (Detailed)

### 1. Lifecycle Management (`lifecycle.py`)
Handles the complete application lifecycle from boot to graceful shutdown. This is the first module executed on startup.

**Key Features**:
- **`lifespan()`**: FastAPI lifespan context manager that orchestrates the entire boot sequence
  - Initializes cognitive pipeline
  - Installs hedge wrap protection
  - Starts background health monitoring
  - Registers all routes and middleware
- **`install_boot()`**: Installs core system protections including:
  - Request validation middleware
  - Error handling wrappers
  - Circuit breaker initialization
- **`background_health_check()`**: Continuous background health monitoring that:
  - Pings all configured AI providers every 30s
  - Checks database connection health
  - Monitors memory and CPU usage
  - Updates health status in real-time
- **`validate_all_providers()`**: Validates all configured AI providers are reachable and authenticated
- **`pr_defense_cleanup()`**: Pull request defense cleanup routines for CI/CD environments

**Boot Sequence**:
1. Load environment configuration
2. Initialize foundation layer
3. Setup resilience and circuit breakers
4. Connect to databases (Redis, Supabase)
5. Validate AI provider credentials
6. Start background health monitors
7. Register middleware and routes
8. Begin accepting requests

---

### 2. Rate Limiting & Tiers (`limits.py`)
Implements tiered rate limiting with quota management for 4 distinct user tiers. All rate limiting is enforced at the middleware level before any request processing.

**Tier Configuration (`TIERS`)**:
| Tier | Requests/min | Tokens/day | Max context | Priority |
|------|--------------|------------|-------------|----------|
| Free | 30 | 300,000 | 8k | Low |
| Pro | 120 | 2,000,000 | 16k | Medium |
| Elite | 300 | 8,000,000 | 32k | High |
| Enterprise | Unlimited | Custom | Custom | Highest |

**Key Classes & Functions**:
- **`RateLimiter`**: Async rate limiter implementation using token bucket algorithm
  - Per-user per-tier rate tracking
  - Sliding window rate calculation
  - Burst allowance for short spikes
- **`check_rate_limit()`**: Validate request against tier limits before processing
- **`check_and_update_quota()`**: Track and update token quota usage with Redis persistence
- **`estimate_tokens()`**: Fast token estimation using tiktoken for request pre-validation

**Quota Enforcement Flow**:
1. Extract user tier from authentication
2. Check rate limit against sliding window
3. Estimate request token usage
4. Verify remaining daily quota
5. Reserve tokens for the request
6. Process request
7. Update quota usage on completion

---

### 3. Middleware (`middleware.py`)
FastAPI middleware stack that runs in sequence for every incoming request. All middleware is registered during boot.

**Middleware Pipeline Order**:
1. **CORS Middleware**: Cross-origin resource sharing configuration
2. **Request ID Middleware**: Assigns unique request ID for tracing
3. **Logging Middleware**: Structured request logging with timing
4. **Rate Limit Middleware**: Enforce tiered rate limits
5. **Auth Middleware**: JWT token validation and user extraction
6. **Quota Middleware**: Token quota reservation
7. **Error Handling Middleware**: Global exception catching

**Key Functions**:
- **`register_middleware()`**: Registers all API middleware in correct order
- **`register_exception_handlers()`**: Global exception handling that converts all errors to consistent JSON format
- **`register_monitor_routes()`**: Health and monitoring endpoints registration
- **`MONITOR_PATHS`**: List of monitoring/health check paths that bypass rate limiting

**Exception Handling**:
All exceptions are caught and converted to a consistent format:
```json
{
  "error": "ErrorType",
  "message": "Human readable message",
  "request_id": "uuid",
  "details": {}
}
```

---

### 4. State Management (`state.py`)
Global application state and caching infrastructure. All state is accessible through a singleton pattern.

**Key Initialization Functions**:
- **`init_db()`**: Initialize Supabase async client with connection pooling
- **`init_redis()`**: Initialize Redis connection with retry logic
- **`init_qstash()`**: Initialize QStash for async task queue

**Redis Cache Operations**:
- **`get_redis_cache()`**: Get cached value with TTL check
- **`set_redis_cache()`**: Set cached value with configurable TTL
- **`delete_redis_cache()`**: Invalidate cached value by key pattern

**Global State Object (`state`)**:
```python
state = {
    "start_time": float,
    "request_count": int,
    "active_requests": int,
    "provider_health": dict,
    "system_health": dict,
    "config": Settings
}
```

**Exception Classes**:
- **`RateLimitExceeded`**: Exception class for rate limit violations
- **`QuotaExceeded`**: Exception class for token quota violations
- **`ProviderUnavailable`**: Exception class for AI provider failures

---

### 5. AI Routing (`ai_router.py`)
Intelligent AI request routing with provider selection, failover, and load balancing. This is the core decision engine for all AI requests.

**Routing Logic**:
1. **Task Classification**: Classify request type (chat, code, reasoning, creative)
2. **Provider Selection**: Select optimal provider based on:
   - Task type specialization
   - Current provider health status
   - User tier and quota
   - Latency history
3. **Failover Chain**: If primary provider fails, automatically failover to next best provider
4. **Load Balancing**: Distribute requests across healthy providers

**Supported Providers**:
- OpenAI (GPT-4o, GPT-4o-mini)
- Anthropic (Claude 3.5 Sonnet, Claude 3 Opus)
- Meta AI (Llama 3.1 405B)
- Nebius NVIDIA (Llama 3, Mistral)
- Local Twin Engine (on-prem inference)

**Key Features**:
- Parallel request execution for improved reliability
- First-token race detection
- Provider health-aware routing
- Automatic fallback chain configuration
- Latency-based provider ranking

---

### 6. Workspace Engine (`workspace_engine.py`)
Workspace management and code analysis for local development environments. Detects and analyzes code repositories.

**Workspace Detection**:
- Auto-detects project type from file structure
- Reads existing configuration files (package.json, pyproject.toml, etc.)
- Builds workspace context from repository structure

**Code Analysis Capabilities**:
- Real-time code diagnostics
- Syntax error detection
- Import dependency analysis
- Code vocabulary extraction
- Semantic context building

**Vocabulary Management**:
- Builds dynamic vocabulary from workspace code
- Tracks domain-specific terminology
- Provides context injection for AI requests

---

### 7. Routes (Detailed)

#### Chat Routes (`routes_chat.py`)
- **POST `/v1/chat/completions`**: Main chat completion endpoint
  - Supports streaming and non-streaming responses
  - Tool calling support
  - Context window management
- **GET `/v1/chat/models`**: List available models and their capabilities
- **POST `/v1/chat/stream`**: WebSocket streaming endpoint

#### Admin Routes (`routes_admin.py`)
- **GET `/admin/health`**: System health status
- **GET `/admin/metrics`**: Request metrics and statistics
- **POST `/admin/reload`**: Reload configuration
- **GET `/admin/providers`**: AI provider health status

#### Operations Routes (`routes_ops.py`)
- **GET `/ops/status`**: Real-time system status
- **GET `/ops/logs`**: Recent request logs
- **POST `/ops/cache/clear`**: Clear cache
- **GET `/ops/trace/{request_id}`**: Request trace details

#### Tools Routes (`routes_tools.py`)
- **POST `/v1/tools/execute`**: Execute tool calls
- **GET `/v1/tools/list`**: List available tools
- **POST `/v1/tools/register`**: Register custom tools

---

## Configuration

The API module is fully configurable through environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AXELR_MAX_TOKENS` | 8192 | Maximum tokens per request |
| `AXELR_TEMPERATURE` | 0.7 | Default model temperature |
| `AXELR_DEBUG` | False | Debug mode flag |
| `AXELR_DEFAULT_MODEL` | gpt-4o | Default AI model |
| `AXELR_CONDUCTOR_DEADLINE_S` | 25.0 | Request deadline in seconds |
| `REDIS_URL` | - | Redis connection URL |
| `SUPABASE_URL` | - | Supabase project URL |
| `SUPABASE_KEY` | - | Supabase API key |

---

## Key Features (Production)

### Performance
- **Async-first design**: All I/O operations are fully async
- **Redis caching**: Semantic and response caching for 40-70% hit rate
- **Connection pooling**: Database and HTTP connection pooling
- **Streaming support**: Server-sent events and WebSocket streaming

### Reliability
- **Circuit breakers**: Per-provider circuit breakers with automatic recovery
- **Failover chains**: Automatic provider failover on failures
- **Health monitoring**: Continuous background health checks
- **Graceful shutdown**: Proper connection cleanup on shutdown

### Security
- **JWT authentication**: Stateless authentication with JWT tokens
- **Rate limiting**: Tiered rate limiting to prevent abuse
- **PII detection**: Automatic PII detection and redaction
- **Input validation**: Full request validation at middleware level

### Observability
- **Request tracing**: Unique request IDs with full trace context
- **Structured logging**: JSON logging with all request details
- **Metrics collection**: Real-time request metrics and statistics
- **Health endpoints**: Comprehensive health check endpoints

---

## Request Flow

```
Incoming Request
    ↓
Middleware Pipeline
    ├─ CORS Check
    ├─ Request ID Assignment
    ├─ Logging
    ├─ Rate Limit Check
    ├─ Auth Validation
    └─ Quota Check
    ↓
Route Handler
    ↓
AI Router
    ├─ Task Classification
    ├─ Provider Selection
    ├─ Routing Decision
    └─ Failover Setup
    ↓
Core Pipeline
    ├─ Context Building
    ├─ Request Processing
    └─ Response Generation
    ↓
Response
```

---

## Deployment

### Production Deployment
The API module is designed for production deployment with:
- Uvicorn ASGI server with workers
- Redis for caching and rate limiting
- Supabase for persistence
- Horizontal scaling support
- Kubernetes-ready configuration

### Performance Characteristics
- **Latency**: ~100ms overhead before AI provider call
- **Throughput**: ~1000 requests/sec per instance
- **Memory**: ~200MB baseline memory usage
- **Uptime**: 99.9%+ with proper configuration

---

## Testing

All API routes are fully testable with pytest:
```bash
pytest api/tests/ -v --cov=api
```

Test coverage targets: 90%+ for all API modules.