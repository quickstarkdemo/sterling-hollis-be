from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import re

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api_traces.context import (
    bind_provisional_trace_context,
    provisional_trace_context,
)
from app.config import Settings, get_settings
from app.observability.llm_otel import initialize_llm_otel
from app.routers.admin_catalog import router as admin_catalog_router
from app.routers.api_traces import router as api_traces_router
from app.routers.admin_synthetic import router as admin_router
from app.routers.catalog import router as catalog_router
from app.routers.chat import router as chat_router
from app.routers.demo_observability import router as demo_observability_router
from app.routers.recommendations import router as rec_router
from app.services.demo_observability import (
    annotate_network_outage_span,
    demo_network_outage_active,
    demo_network_outage_response_payload,
    log_network_outage_block,
)
from app.services.auth.admin import require_catalog_admin


OAI_SANDBOX_ORIGIN_RE = re.compile(r"^https://.*\.oaiusercontent\.com$")
CORS_ALLOWED_HEADERS = [
    "Accept",
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "Last-Event-Id",
    "MCP-Protocol-Version",
    "MCP-Session-Id",
    "traceparent",
    "tracestate",
    "X-Client-Request-Id",
    "X-Requested-With",
    "X-Trace-Surface",
]
CORS_EXPOSED_HEADERS = [
    "traceparent",
    "tracestate",
    "X-Trace-Id",
    "X-Trace-Span-Id",
    "X-Trace-Capture",
    "MCP-Session-Id",
]

DEMO_NETWORK_OUTAGE_EXEMPT_PREFIXES = (
    "/admin/demo/observability",
    "/api/demo/observability",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/ui-assets",
    "/mcp",
)
DEMO_NETWORK_OUTAGE_BLOCKED_PREFIXES = (
    "/api/",
    "/recommendations/",
    "/feeds/",
)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().rstrip("/") for item in value.split(",") if item.strip()]


def _cors_allowed_origins(settings) -> list[str]:
    origins = [*_split_csv(settings.cors_allowed_origins), settings.public_base_url.rstrip("/")]
    return list(dict.fromkeys(origin for origin in origins if origin))


def get_widget_state(token: str) -> dict:
    from app.services.apps_ui import get_widget_state as _get_widget_state

    return _get_widget_state(token)


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


def _demo_network_outage_should_block(path: str, *, product_image_path: str) -> bool:
    if path in {"/api", "/recommendations", "/feeds"}:
        return True
    if path.startswith(product_image_path.rstrip("/") + "/"):
        return False
    if path.startswith(DEMO_NETWORK_OUTAGE_EXEMPT_PREFIXES):
        return False
    return path.startswith(DEMO_NETWORK_OUTAGE_BLOCKED_PREFIXES)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    initialize_llm_otel()
    fashion_mcp = None
    if settings.enable_mcp_adapter:
        from app.mcp_server import mcp as fashion_mcp

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if fashion_mcp is None:
            yield
            return
        async with fashion_mcp.session_manager.run():
            yield

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    allow_origin_regex = r"https://.*\.oaiusercontent\.com" if settings.enable_openai_apps_ui else None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_allowed_origins(settings),
        allow_origin_regex=allow_origin_regex,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=CORS_ALLOWED_HEADERS,
        expose_headers=CORS_EXPOSED_HEADERS,
    )
    static_dir = Path(__file__).resolve().parent / "static" / "chatgpt-ui"
    product_image_dir = Path(settings.product_image_output_dir)
    product_image_dir.mkdir(parents=True, exist_ok=True)
    product_image_url_path = settings.product_image_url_path.rstrip("/") or "/product-images"

    @app.middleware("http")
    async def api_trace_context_middleware(request: Request, call_next):
        if not settings.api_trace_capture_enabled or request.method == "OPTIONS":
            return await call_next(request)
        provisional = provisional_trace_context(
            request.headers.get("traceparent"),
            request.headers.get("tracestate"),
        )
        request.state.api_trace_provisional = provisional
        with bind_provisional_trace_context(provisional):
            response = await call_next(request)
        response.headers["traceparent"] = provisional.traceparent
        if provisional.tracestate:
            response.headers["tracestate"] = provisional.tracestate
        capture = getattr(request.state, "api_trace_capture", None)
        if capture and capture.authorized:
            response.headers["X-Trace-Id"] = provisional.trace_id
            response.headers["X-Trace-Span-Id"] = provisional.span_id
            response.headers["X-Trace-Capture"] = "active"
        return response

    @app.middleware("http")
    async def demo_network_outage_middleware(request: Request, call_next):
        path = request.url.path
        if (
            request.method == "OPTIONS"
            or not demo_network_outage_active()
            or not _demo_network_outage_should_block(path, product_image_path=product_image_url_path)
        ):
            return await call_next(request)

        annotate_network_outage_span(path=path, method=request.method)
        log_network_outage_block(path=path, method=request.method)
        return JSONResponse(
            demo_network_outage_response_payload(),
            status_code=503,
            headers={"Retry-After": "30"},
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": settings.app_build_version or "dev"}

    if settings.enable_openai_apps_ui:
        @app.get("/ui-assets/session/{token}.json")
        def widget_session(token: str, request: Request) -> JSONResponse:
            headers = _widget_cors_headers(request.headers.get("origin"), settings.public_base_url)
            try:
                return JSONResponse(get_widget_state(token), headers=headers)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    app.include_router(admin_catalog_router)
    app.include_router(api_traces_router)
    if settings.enable_legacy_admin_routes:
        legacy_dependencies = (
            [Depends(require_catalog_admin)]
            if settings.environment.strip().lower() in {"prod", "production"}
            else []
        )
        app.include_router(admin_router, dependencies=legacy_dependencies)
    app.include_router(demo_observability_router)
    app.include_router(catalog_router)
    app.include_router(chat_router)
    app.include_router(rec_router)
    app.mount(
        product_image_url_path,
        StaticFiles(directory=product_image_dir, check_dir=False),
        name="product-images",
    )
    if settings.enable_openai_apps_ui:
        app.mount("/ui-assets", StaticFiles(directory=static_dir), name="ui-assets")
    if fashion_mcp is not None:
        app.mount("/mcp", fashion_mcp.streamable_http_app())
    return app


app = create_app()
