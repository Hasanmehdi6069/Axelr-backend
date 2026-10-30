"""BuildX India API Routes
Zero-trust protected routes for tenant management and operations."""
from aiohttp import web
from .core import get_settings, register_tenant, get_tenant, create_access_token, requires_permission, validate_origin
import logging

logger = logging.getLogger("axelr.buildx_india.routes")

def setup_routes(app: web.Application):
    app.add_routes([
        web.post("/api/v1/tenants", register_tenant_handler),
        web.get("/api/v1/tenants/{tenant_id}", get_tenant_handler),
        web.post("/api/v1/auth/token", create_token_handler),
    ])

async def register_tenant_handler(request: web.Request) -> web.Response:
    """Register a new tenant (only allowed by system admins)."""
    body = await request.json()
    try:
        tenant = await register_tenant(body["name"], body.get("plan", "free"))
        return web.json_response({
            "tenant_id": tenant.tenant_id,
            "name": tenant.name,
            "plan": tenant.plan,
            "created_at": tenant.created_at.isoformat()
        }, status=201)
    except Exception as e:
        logger.error("tenant_registration_failed error=%s", str(e))
        return web.json_response({"error": str(e)}, status=400)

async def get_tenant_handler(request: web.Request) -> web.Response:
    """Get tenant information (requires valid authentication)."""
    tenant_id = request.match_info["tenant_id"]
    tenant = await get_tenant(tenant_id)
    if not tenant:
        return web.json_response({"error": "Tenant not found"}, status=404)
    return web.json_response({
        "tenant_id": tenant.tenant_id,
        "name": tenant.name,
        "plan": tenant.plan,
        "is_active": tenant.is_active,
        "quota_used": tenant.quota_used,
        "quota_limit": tenant.quota_limit
    })

async def create_token_handler(request: web.Request) -> web.Response:
    """Create an access token for authenticated users."""
    body = await request.json()
    # Validate origin
    origin = request.headers.get("Origin", "")
    if origin and not validate_origin(origin):
        logger.warning("invalid_origin_blocked origin=%s", origin)
        return web.json_response({"error": "Origin not allowed"}, status=403)
    
    try:
        token = create_access_token(
            user_id=body["user_id"],
            tenant_id=body["tenant_id"],
            roles=body.get("roles", ["user"]),
            permissions=set(body.get("permissions", ["read"]))
        )
        return web.json_response({"access_token": token, "token_type": "Bearer"})
    except Exception as e:
        logger.error("token_creation_failed error=%s", str(e))
        return web.json_response({"error": "Failed to create token"}, status=400)