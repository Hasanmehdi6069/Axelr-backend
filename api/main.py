from fastapi import FastAPI
from .config import ORIGIN
from .lifecycle import lifespan, attach_boot
from .middleware import register_middleware, register_exception_handlers, register_monitor_routes
from .routes_ops import router as ops_router
from .qstash_webhook import router as qstash_router

app = FastAPI(lifespan=lifespan, title="AXELR API", version="27.0")

register_middleware(app, allowed_origins=[ORIGIN, "http://localhost:3000"])
register_exception_handlers(app)
register_monitor_routes(app)

app.include_router(ops_router)
app.include_router(qstash_router)

attach_boot(app)