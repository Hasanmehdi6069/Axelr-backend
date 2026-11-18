# Vultr Cloud Module - Production Documentation

**Module ID**: vultr_cloud  
**Status**: Production Ready  
**Last Updated**: Master production finalization

---

## Overview

Vultr Cloud is a cloud provisioning module that provides cost-guarded instance launch, automated scaling, and multi-region deployment. This module integrates AXELR AI's core Resilience Layer with Vultr Cloud API for cost-aware, reliable cloud infrastructure management.

## Core Capabilities

- **Cost-Guarded Launch**: Real-time budget enforcement before instance creation
- **Auto-scaling**: Queue-based dynamic instance scaling
- **Multi-region Deployment**: Geographic instance placement optimization
- **Instance Lifecycle**: Create, start, stop, terminate operations
- **Cost Tracking**: Hourly cost projection and budget alerts

## Implementation Details

### Architecture Flow
```
1. Provisioning request arrives with instance requirements
2. Cost guard calculates projected hourly cost
3. Budget check passes/fails based on configured limits
4. Vultr API call creates instance with retry logic
5. Instance state is persisted to database
6. Auto-scaler adjusts pool based on queue depth
```

### Key Components
- `VultrClient`: Vultr API v2 wrapper with circuit breaker
- `CostGuard`: Real-time budget calculation and enforcement
- `AutoScaler`: Queue-based dynamic scaling logic
- `InstancePool`: Managed instance pool with health checks
- `CostProjector`: Hourly cost projection and alerting

## Run Instructions

### Prerequisites
- Vultr Cloud API key with instance provisioning permissions
- Minimum 1GB RAM for instance pool management
- Network access to Vultr API endpoints

### Configuration
```env
# Vultr Cloud Settings
VULTR_API_KEY=your_api_key
VULTR_REGION=blr
VULTR_PLAN=vc2-1c-1gb
VULTR_MAX_INSTANCES=10
VULTR_HOURLY_BUDGET=10.0
VULTR_AUTOSCALER_ENABLED=true
VULTR_QUEUE_THRESHOLD=50
```

### Start Module
```bash
# Run module health check
python -c "from modules.vultr_cloud import VultrClient; print('Vultr Cloud OK:', VultrClient().health_check())"

# Start auto-scaler independently
python -m modules.vultr_cloud.autoscaler &

# Module is automatically loaded with core API server
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

### API Endpoints
- `POST /api/v1/vultr/instances` - Provision new instance
- `GET /api/v1/vultr/instances` - List all instances
- `POST /api/v1/vultr/instances/{id}/start` - Start instance
- `POST /api/v1/vultr/instances/{id}/stop` - Stop instance
- `DELETE /api/v1/vultr/instances/{id}` - Terminate instance
- `GET /api/v1/vultr/cost` - Current cost projection

## Performance Characteristics
- **Instance Provisioning**: 30-60 seconds (cloud API)
- **Cost Calculation**: <1ms (local lookup)
- **Auto-scaling Decision**: <5ms (queue analysis)
- **API Call Retry**: 3 retries with exponential backoff

---