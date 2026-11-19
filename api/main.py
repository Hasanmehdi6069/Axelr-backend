from fastapi import FastAPI

from .config import ORIGIN
from .enhancer import router as enhancer_router
from .lifecycle import attach_boot, lifespan
from .middleware import register_exception_handlers, register_middleware, register_monitor_routes
from .qstash_webhook import router as qstash_router
from .routes_ops import router as ops_router
from .routes_chat import router as chat_router
from .routes_tools import router as tools_router
from .routes_admin import router as admin_router

app = FastAPI(lifespan=lifespan, title="AXELR API", version="27.0")

register_middleware(app, allowed_origins=[ORIGIN, "http://localhost:3000"])
register_exception_handlers(app)
register_monitor_routes(app)

app.include_router(ops_router)
app.include_router(qstash_router)
app.include_router(chat_router)
app.include_router(tools_router)
app.include_router(admin_router)
app.include_router(enhancer_router)

attach_boot(app)