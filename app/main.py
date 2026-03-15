from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.mcp_server import mcp as fashion_mcp
from app.routers.admin_synthetic import router as admin_router
from app.routers.recommendations import router as rec_router
from app.services.apps_ui import get_widget_state


OAI_SANDBOX_ORIGIN_RE = re.compile(r"^https://.*\.oaiusercontent\.com$")


def _widget_cors_headers(origin: str | None, public_base_url: str) -> dict[str, str]:
    if not origin:
        return {}

    normalized_public_base = public_base_url.rstrip("/")
    if origin in {
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        normalized_public_base,
    } or OAI_SANDBOX_ORIGIN_RE.match(origin):
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Vary": "Origin",
        }
    return {}


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with fashion_mcp.session_manager.run():
            yield

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            settings.public_base_url.rstrip("/"),
        ],
        allow_origin_regex=r"https://.*\.oaiusercontent\.com",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    static_dir = Path(__file__).resolve().parent / "static" / "chatgpt-ui"

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": settings.app_build_version or "dev"}

    @app.get("/ui-assets/session/{token}.json")
    def widget_session(token: str, request: Request) -> JSONResponse:
        headers = _widget_cors_headers(request.headers.get("origin"), settings.public_base_url)
        try:
            return JSONResponse(get_widget_state(token), headers=headers)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    app.include_router(admin_router)
    app.include_router(rec_router)
    app.mount("/ui-assets", StaticFiles(directory=static_dir), name="ui-assets")
    app.mount("/mcp", fashion_mcp.streamable_http_app())
    return app


app = create_app()
