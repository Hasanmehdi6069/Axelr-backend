"""API Routes for Nebius × NVIDIA Inference Bridge
Exposes REST endpoints for streaming inference, model optimization, and bridge metrics.
Integrates with Axelr's core API framework and authentication middleware.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, AsyncIterator
from aiohttp import web
import json

from .core import (
    init, is_configured, stream_inference, optimize_nvidia_model,
    get_rate_limiter, get_connection_pool, get_settings, __version__
)

logger = logging.getLogger("axelr.nebius_nvidia.routes")

# ──────────────────────────────────────────────────────────────────────────────
# Request Schemas (would use Pydantic in production for full validation)
# ──────────────────────────────────────────────────────────────────────────────
INFERENCE_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["prompt"],
    "properties": {
        "prompt": {"type": "string", "minLength": 1},
        "model": {"type": "string"},
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 8192},
        "temperature": {"type": "number", "minimum": 0.0, "maximum": 2.0},
        "preferred_region": {"type": "string"}
    },
    "additionalProperties": False
}

# ──────────────────────────────────────────────────────────────────────────────
# Route Handlers
# ──────────────────────────────────────────────────────────────────────────────
async def handle_health_check(request: web.Request) -> web.Response:
    """Health check endpoint for the Nebius × NVIDIA bridge."""
    if not is_configured():
        return web.json_response({
            "status": "unhealthy",
            "reason": "missing_configuration",
            "version": __version__
        }, status=503)
    
    pool = get_connection_pool()
    region_statuses = {}
    for region, cb in pool.circuit_breakers.items():
        region_statuses[region] = {
            "is_open": cb.is_open,
            "failure_count": cb.failure_count,
            "success_count": cb.success_count
        }
    
    return web.json_response({
        "status": "healthy",
        "version": __version__,
        "regions": region_statuses,
        "active_connections": len([c for c in pool.connections if c.in_use]),
        "max_connections": pool.max_connections
    })

async def handle_stream_inference(request: web.Request) -> web.StreamResponse:
    """Streaming inference endpoint that returns tokens as they're generated."""
    if not is_configured():
        return web.json_response({"error": "bridge_unconfigured"}, status=503)
    
    # Parse and validate request body
    try:
        body = await request.json()
        # Minimal validation (would use Pydantic in production)
        if not body.get("prompt"):
            return web.json_response({"error": "prompt_required"}, status=400)
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)
    
    # Extract parameters with defaults
    prompt = body["prompt"]
    model = body.get("model")
    max_tokens = body.get("max_tokens", 4096)
    temperature = body.get("temperature", 0.7)
    preferred_region = body.get("preferred_region")
    
    # Prepare streaming response
    response = web.StreamResponse()
    response.headers['Content-Type'] = 'text/event-stream'
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    await response.prepare(request)
    
    # Stream tokens to client
    try:
        async for token_chunk in stream_inference(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            preferred_region=preferred_region
        ):
            # Format as SSE event
            event_data = json.dumps({"text": token_chunk})
            await response.write(f"data: {event_data}\n\n".encode('utf-8'))
            await asyncio.sleep(0.001)  # Prevent overwhelming the client
        
        # Send completion event
        await response.write(b"data: {\"done\": true}\n\n")
        await response.done()
        
    except Exception as e:
        logger.error("streaming_inference_failed error=%s", str(e))
        error_data = json.dumps({"error": str(e)})
        await response.write(f"data: {error_data}\n\n".encode('utf-8'))
        await response.done()
    
    return response

async def handle_optimize_model(request: web.Request) -> web.Response:
    """Endpoint to trigger TensorRT optimization for a specific model."""
    if not is_configured():
        return web.json_response({"error": "bridge_unconfigured"}, status=503)
    
    try:
        body = await request.json()
        model_name = body.get("model")
        if not model_name:
            return web.json_response({"error": "model_name_required"}, status=400)
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)
    
    # Run optimization
    result = await optimize_nvidia_model(model_name)
    status = 200 if result["status"] == "success" else 500
    return web.json_response(result, status=status)

async def handle_get_metrics(request: web.Request) -> web.Response:
    """Get current metrics for rate limiting and connection pool."""
    if not is_configured():
        return web.json_response({"error": "bridge_unconfigured"}, status=503)
    
    rate_limiter = get_rate_limiter()
    pool = get_connection_pool()
    settings = get_settings()
    
    metrics = {
        "rate_limiter": {
            "current_tokens": rate_limiter.current_tokens,
            "max_tokens": rate_limiter.max_bucket_size,
            "rate_tpm": rate_limiter.rate
        },
        "connection_pool": {
            "active_connections": len([c for c in pool.connections if c.in_use]),
            "total_connections": len(pool.connections),
            "max_connections": pool.max_connections
        },
        "configured_models": settings.NVIDIA_MODEL_NAMES,
                "configured_regions": settings.NEBIUS_REGIONS
    }
    
    return web.json_response(metrics)

# ──────────────────────────────────────────────────────────────────────────────
# Route Registration
# ──────────────────────────────────────────────────────────────────────────────
def register_routes(app: web.Application, prefix: str = "/api/nebius") -> None:
    """Register all Nebius bridge routes with the main application."""
    routes = web.RouteTableDef()
    
    # Register endpoints
    routes.get(f"{prefix}/health")(handle_health_check)
    routes.post(f"{prefix}/inference")(handle_stream_inference)
    routes.post(f"{prefix}/optimize")(handle_optimize_model)
    routes.get(f"{prefix}/metrics")(handle_get_metrics)
    
    # Add routes to app
    app.add_routes(routes)
    logger.info("nebius_nvidia_routes_registered prefix=%s", prefix)

# ──────────────────────────────────────────────────────────────────────────────
# Lifecycle Management
# ──────────────────────────────────────────────────────────────────────────────
async def on_startup(app: web.Application) -> None:
    """Initialize the bridge on application startup."""
    await init()
    logger.info("nebius_nvidia_bridge_started")

async def on_shutdown(app: web.Application) -> None:
    """Cleanup resources on application shutdown."""
    from .core import shutdown
    await shutdown()
    pool = get_connection_pool()
    # Close all active HTTP sessions
    for conn in pool.connections:
        await conn.session.close()
    logger.info("nebius_nvidia_bridge_shutdown_completed")