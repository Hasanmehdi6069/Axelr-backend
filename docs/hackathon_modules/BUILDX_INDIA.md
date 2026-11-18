# BuildX India Module - Production Documentation

**Module ID**: buildx_india  
**Status**: Production Ready  
**Last Updated**: Master production finalization

---

## Overview

BuildX India is a zero-trust security platform that provides multi-tenant authentication, role-based access control, and audit logging. This module builds on top of the core Exception and Resilience Layers to provide secure, isolated multi-tenant operations for Indian market deployments.

## Core Capabilities

- **Zero-Trust Authentication**: JWT + mTLS multi-factor authentication
- **Multi-Tenant Isolation**: Strict tenant-level data isolation with row-level security
- **Role-Based Access Control**: Granular permission system with custom roles
- **Indian Market Compliance**: GDPR + Indian DPDP Act compliance
- **Security Audit Logging**: Immutable security event trail

## Implementation Details

### Architecture Flow
```
1. Request arrives with authentication credentials
2. Zero-trust validator verifies JWT + mTLS certificate
3. Tenant context is extracted and validated
4. RBAC engine checks permission for requested operation
5. Row-level security filters apply to all database queries
6. Security event is logged to immutable audit trail
```

### Key Components
- `ZeroTrustAuth`: JWT + mTLS multi-factor authentication
- `TenantIsolator`: Strict row-level security enforcement
- `RBACEngine`: Granular role-based permission system
- `ComplianceValidator`: DPDP Act compliance checks
- `SecurityAuditLog`: Immutable security event logging

## Run Instructions

### Prerequisites
- SSL/TLS certificates for mTLS
- JWT signing keys (RSA 2048+ bit)
- PostgreSQL 14+ with row-level security support
- Minimum 1GB RAM for security operations

### Configuration
```env
# BuildX India Settings
BUILDX_JWT_PUBLIC_KEY=./keys/jwt_public.pem
BUILDX_MTLS_REQUIRED=true
BUILDX_TENANT_ISOLATION_STRICT=true
BUILDX_DPDP_COMPLIANCE=true
BUILDX_AUDIT_LOG_ENABLED=true
BUILDX_RATE_LIMIT_AUTH=10/minute
```

### Start Module
```bash
# Run module health check
python -c "from modules.buildx_india import ZeroTrustAuth; print('BuildX India OK:', ZeroTrustAuth().health_check())"

# Module is automatically loaded with core API server
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

### API Endpoints
- `POST /api/v1/buildx/auth/login` - Tenant authentication
- `POST /api/v1/buildx/auth/mfa` - Multi-factor verification
- `GET /api/v1/buildx/roles` - List tenant roles
- `POST /api/v1/buildx/roles` - Create custom role
- `GET /api/v1/buildx/audit` - Security audit log

## Performance Characteristics
- **Authentication**: <10ms (JWT + mTLS verification)
- **RBAC Check**: <1ms (in-memory permission lookup)
- **Tenant Isolation**: 0 overhead (database row-level)
- **Audit Log Write**: <5ms (immutable storage)

---