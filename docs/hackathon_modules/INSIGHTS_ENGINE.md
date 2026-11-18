# Insights Engine Module - Production Documentation

**Module ID**: insights_engine  
**Status**: Production Ready  
**Last Updated**: Master production finalization

---

## Overview

Insights Engine is a data platform module that provides MongoDB audit logging, real-time analytics, and insight generation. This module builds on top of the core Persistence Layer to provide structured audit trails and actionable business insights from all system operations.

## Core Capabilities

- **MongoDB Audit Logging**: Immutable audit trail of all system operations
- **Real-time Analytics**: Streaming analytics with windowed aggregations
- **Insight Generation**: AI-powered pattern detection and insight extraction
- **Query Interface**: SQL-like query API for audit data
- **Retention Policies**: Automatic data lifecycle management

## Implementation Details

### Architecture Flow
```
1. System operation completes with metadata
2. Audit event is created with immutable timestamp
3. Event is written to MongoDB with write concern majority
4. Analytics pipeline processes event in real-time
5. Insight generator detects patterns from event stream
6. Insights are stored and exposed via query API
```

### Key Components
- `AuditLogger`: Immutable event logging with MongoDB
- `AnalyticsPipeline`: Windowed streaming aggregation engine
- `InsightGenerator`: Pattern detection using Sovereign Engine
- `QueryEngine`: SQL-like audit data query interface
- `RetentionManager`: Automatic data lifecycle enforcement

## Run Instructions

### Prerequisites
- MongoDB 5.0+ running with replica set (for write concern)
- Minimum 4GB RAM for analytics operations
- Network access to MongoDB instance

### Configuration
```env
# Insights Engine Settings
INSIGHTS_MONGODB_URI=mongodb://localhost:27017
INSIGHTS_DB_NAME=axelr_insights
INSIGHTS_AUDIT_ENABLED=true
INSIGHTS_ANALYTICS_ENABLED=true
INSIGHTS_RETENTION_DAYS=90
INSIGHTS_WRITE_CONCERN=majority
```

### Start Module
```bash
# Run module health check
python -c "from modules.insights_engine import AuditLogger; print('Insights Engine OK:', AuditLogger().health_check())"

# Start analytics pipeline independently
python -m modules.insights_engine.analytics &

# Module is automatically loaded with core API server
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

### API Endpoints
- `GET /api/v1/insights/audit` - Query audit log
- `GET /api/v1/insights/analytics` - Real-time analytics
- `GET /api/v1/insights/generated` - Generated insights
- `POST /api/v1/insights/query` - Custom audit query
- `GET /api/v1/insights/metrics` - System metrics overview

## Performance Characteristics
- **Audit Log Write**: <10ms (MongoDB majority concern)
- **Analytics Processing**: <5ms per event (streaming)
- **Insight Generation**: <100ms (Sovereign Engine local)
- **Query Latency**: <50ms (indexed audit queries)

---