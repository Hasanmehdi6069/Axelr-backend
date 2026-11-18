# PayPal Module - Production Documentation

**Module ID**: paypal  
**Status**: Production Ready  
**Last Updated**: Master production finalization

---

## Overview

PayPal is a FinTech integration module that provides Orders v2 API integration, webhook verification, and payment lifecycle management. This module builds on top of the core Resilience Layer for idempotent payment operations with automatic retry and circuit breaker protection.

## Core Capabilities

- **Orders v2 API**: Full PayPal Orders v2 integration (create, capture, refund)
- **Webhook Verification**: Cryptographically verified webhook signatures
- **Idempotent Operations**: Idempotency key enforced for all write operations
- **Payment Lifecycle**: Complete payment state machine management
- **Fraud Detection**: Basic fraud pattern detection and risk scoring

## Implementation Details

### Architecture Flow
```
1. Payment request arrives with idempotency key
2. Resilience layer validates request and rate limits
3. PayPal Orders v2 API call is made with circuit breaker
4. Payment state is persisted to SQLite
5. Webhook listener receives and verifies updates
6. Payment state machine transitions accordingly
```

### Key Components
- `PayPalClient`: Orders v2 API wrapper with retry logic
- `WebhookVerifier`: ECDSA signature verification
- `PaymentStateMachine`: Finite state machine for payment lifecycle
- `IdempotencyStore`: Redis/SQLite idempotency key storage
- `RiskScorer`: Basic fraud detection and risk assessment

## Run Instructions

### Prerequisites
- PayPal Developer account with Orders v2 permissions
- Client ID and Client Secret from PayPal Dashboard
- Webhook endpoint publicly accessible (for production)
- Minimum 512MB RAM for payment operations

### Configuration
```env
# PayPal Settings
PAYPAL_ENV=sandbox
PAYPAL_CLIENT_ID=your_client_id
PAYPAL_CLIENT_SECRET=your_client_secret
PAYPAL_WEBHOOK_ID=your_webhook_id
PAYPAL_WEBHOOK_SECRET=your_webhook_secret
PAYPAL_MAX_RETRIES=3
PAYPAL_IDEMPOTENCY_TTL=86400
```

### Start Module
```bash
# Run module health check
python -c "from modules.paypal import PayPalClient; print('PayPal OK:', PayPalClient().health_check())"

# Start webhook listener independently
python -m modules.paypal.webhook &

# Module is automatically loaded with core API server
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

### API Endpoints
- `POST /api/v1/paypal/orders` - Create new order
- `GET /api/v1/paypal/orders/{id}` - Get order status
- `POST /api/v1/paypal/orders/{id}/capture` - Capture payment
- `POST /api/v1/paypal/refunds` - Refund captured payment
- `POST /api/v1/paypal/webhook` - Webhook endpoint (PayPal only)

## Performance Characteristics
- **Order Creation**: <150ms (API call)
- **Webhook Verification**: <5ms (cryptographic)
- **Idempotency Check**: <1ms (local lookup)
- **Payment State Transition**: <2ms (local)

---