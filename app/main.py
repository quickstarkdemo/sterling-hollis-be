from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.mcp_server import mcp as fashion_mcp
from app.routers.admin_synthetic import router as admin_router
from app.routers.recommendations import router as rec_router
from app.services.apps_ui import get_widget_state


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with fashion_mcp.session_manager.run():
            yield

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    static_dir = Path(__file__).resolve().parent / "static" / "chatgpt-ui"

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ui-assets/session/{token}.json")
    def widget_session(token: str) -> dict:
        try:
            return get_widget_state(token)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    app.include_router(admin_router)
    app.include_router(rec_router)
    app.mount("/ui-assets", StaticFiles(directory=static_dir), name="ui-assets")
    app.mount("/mcp", fashion_mcp.streamable_http_app())
    return app


app = create_app()
