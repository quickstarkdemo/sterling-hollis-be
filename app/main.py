from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.mcp_server import mcp as fashion_mcp
from app.routers.admin_synthetic import router as admin_router
from app.routers.recommendations import router as rec_router


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with fashion_mcp.session_manager.run():
            yield

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(admin_router)
    app.include_router(rec_router)
    app.mount("/mcp", fashion_mcp.streamable_http_app())
    return app


app = create_app()
