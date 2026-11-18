# AI Genesis Module - Production Documentation

**Module ID**: ai_genesis  
**Status**: Production Ready  
**Last Updated**: Master production finalization

---

## Overview

AI Genesis is a model registry module that provides memory-pressure eviction, versioned model storage, and dynamic model loading. This module builds on top of the core Memory and Sovereign Engine Layers to provide efficient, memory-aware model lifecycle management.

## Core Capabilities

- **Versioned Model Registry**: Semantically versioned model storage with metadata
- **Memory-Pressure Eviction**: LRU + memory-pressure based model unloading
- **Dynamic Loading**: On-demand model loading with priority queue
- **Model Validation**: Automatic model hash verification on load
- **Storage Tiering**: Hot/cold storage tiering for model weights

## Implementation Details

### Architecture Flow
```
1. Model inference request arrives
2. Model registry checks if model is loaded in memory
3. If not loaded, priority queue determines load order
4. Memory pressure evaluator evicts lowest priority models
5. Model weights are loaded and hash verified
6. Inference runs and results are returned
```

### Key Components
- `ModelRegistry`: Versioned model metadata storage
- `MemoryPressureEvictor`: LRU + memory-aware eviction logic
- `DynamicModelLoader`: On-demand model loading with priority
- `ModelValidator`: SHA256 hash verification on load
- `StorageTierer`: Hot/cold storage tiering management

## Run Instructions

### Prerequisites
- Minimum 8GB RAM (configurable based on model sizes)
- Fast SSD storage for hot model tier
- Network storage for cold model tier (optional)

### Configuration
```env
# AI Genesis Settings
AI_GENESIS_REGISTRY_PATH=./models/registry
AI_GENESIS_HOT_STORAGE_PATH=./models/hot
AI_GENESIS_COLD_STORAGE_PATH=./models/cold
AI_GENESIS_MAX_MEMORY_MB=8192
AI_GENESIS_EVICTION_THRESHOLD=0.85
AI_GENESIS_PRIORITY_QUEUE_ENABLED=true
```

### Start Module
```bash
# Run module health check
python -c "from modules.ai_genesis import ModelRegistry; print('AI Genesis OK:', ModelRegistry().health_check())"

# Module is automatically loaded with core API server
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

### API Endpoints
- `POST /api/v1/genesis/models` - Register new model version
- `GET /api/v1/genesis/models` - List all registered models
- `GET /api/v1/genesis/models/{id}` - Model metadata
- `POST /api/v1/genesis/models/{id}/load` - Force load model
- `GET /api/v1/genesis/status` - Registry memory status

## Performance Characteristics
- **Model Load Time**: 1-10 seconds (depends on model size)
- **Eviction Decision**: <1ms (memory pressure calculation)
- **Model Lookup**: <1ms (in-memory registry)
- **Hash Verification**: <100ms per model (SHA256)

---