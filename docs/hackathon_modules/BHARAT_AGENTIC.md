# Bharat Agentic Module - Production Documentation

**Module ID**: bharat_agentic  
**Status**: Production Ready  
**Last Updated**: Master production finalization

---

## Overview

Bharat Agentic is a resilience framework module that provides SQLite WAL mode, regional compliance, and Indian language optimization. This module builds on top of the core Resilience and Persistence Layers to provide reliable, region-optimized operations for Indian deployments.

## Core Capabilities

- **SQLite WAL Mode**: Write-ahead logging for maximum concurrency
- **Regional Resilience**: India-specific network and power failure resilience
- **Indian Language Support**: Hindi + regional language tokenization
- **DPDP Compliance**: Data protection compliance for Indian users
- **Offline Resilience**: Full operation during extended network outages

## Implementation Details

### Architecture Flow
```
1. Operation request arrives with regional context
2. Resilience layer applies India-specific failure guards
3. SQLite WAL mode handles concurrent writes efficiently
4. If language is regional, Sovereign Engine applies optimized tokenization
5. DPDP compliance checks pass before data processing
6. Results are returned with regional formatting
```

### Key Components
- **SQLiteWALManager**: Write-ahead logging configuration and management
- **RegionalResilience**: India-specific failure detection and recovery
- **IndianLanguageProcessor**: Hindi + regional language tokenization
- **DPDPCompliance**: Data protection compliance enforcement
- **OfflineMode**: Extended network outage resilience

## Run Instructions

### Prerequisites
- SQLite 3.37+ with WAL mode support
- Indian language tokenizer models (optional, for regional support)
- Minimum 1GB RAM for resilience operations

### Configuration
```env
# Bharat Agentic Settings
BHARAT_SQLITE_WAL_ENABLED=true
BHARAT_REGIONAL_RESILIENCE=true
BHARAT_INDIAN_LANG_SUPPORT=true
BHARAT_DPDP_COMPLIANCE=true
BHARAT_OFFLINE_MODE_ENABLED=true
BHARAT_OFFLINE_TTL_HOURS=72
```

### Start Module
```bash
# Run module health check
python -c "from modules.bharat_agentic import SQLiteWALManager; print('Bharat Agentic OK:', SQLiteWALManager().health_check())"

# Module is automatically loaded with core API server
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

### API Endpoints
- `GET /api/v1/bharat/resilience` - Resilience status
- `POST /api/v1/bharat/translate` - Regional language translation
- `GET /api/v1/bharat/compliance` - DPDP compliance status
- `GET /api/v1/bharat/offline` - Offline mode status
- `POST /api/v1/bharat/sqlite/optimize` - WAL optimization

## Performance Characteristics
- **SQLite Write**: <2ms (WAL mode concurrent)
- **Regional Tokenization**: <10ms (local Sovereign Engine)
- **Resilience Check**: <1ms (regional guard evaluation)
- **Offline Operation**: 0 overhead (local execution)

---