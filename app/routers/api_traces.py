from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import datetime
import json
import logging
import re
import time
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.api_traces.context import TraceCaptureContext
from app.api_traces.schemas import (
    ApiTraceProjection,
    ClientTraceEventInput,
    TraceEventPage,
    TraceEventProjection,
    TraceListResponse,
)
from app.api_traces.service import (
    append_client_trace_event,
    get_trace_events,
    get_trace_projection,
    list_trace_summaries,
)
from app.config import Settings, get_settings
from app.database import get_db
from app.services.auth.admin import require_api_trace_capture


router = APIRouter(prefix="/api/admin/traces", tags=["admin-api-traces"])
logger = logging.getLogger(__name__)
_QUERY_CREDENTIALS = {"access_token", "authorization", "bearer", "token"}
TraceId = Annotated[
    str,
    Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"),
]


def _encode_cursor(value: tuple[datetime, str] | None) -> str | None:
    if value is None:
        return None
    raw = json.dumps([value[0].isoformat(), value[1]], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str | None) -> tuple[datetime, str] | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        created_at, trace_id = json.loads(base64.urlsafe_b64decode(padded).decode())
        parsed = datetime.fromisoformat(created_at)
        if parsed.tzinfo is None or not isinstance(trace_id, str) or not trace_id:
            raise ValueError
        return parsed, trace_id
    except (
        ValueError,
        TypeError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        binascii.Error,
    ) as exc:
        raise HTTPException(status_code=422, detail="Invalid trace cursor.") from exc


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Trace not found.")


@router.get("", response_model=TraceListResponse)
def list_api_traces(
    response: Response,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    context: TraceCaptureContext = Depends(require_api_trace_capture),
    db: Session = Depends(get_db),
) -> TraceListResponse:
    response.headers["Cache-Control"] = "private, no-store"
    items, next_cursor = list_trace_summaries(
        db,
        owner_provider=context.owner_provider,
        owner_provider_user_id=context.owner_provider_user_id,
        limit=limit,
        cursor=_decode_cursor(cursor),
    )
    return TraceListResponse(items=items, next_cursor=_encode_cursor(next_cursor))


@router.get("/{trace_id}", response_model=ApiTraceProjection)
def get_api_trace(
    trace_id: TraceId,
    response: Response,
    context: TraceCaptureContext = Depends(require_api_trace_capture),
    db: Session = Depends(get_db),
) -> ApiTraceProjection:
    response.headers["Cache-Control"] = "private, no-store"
    projection = get_trace_projection(
        db,
        trace_id=trace_id,
        owner_provider=context.owner_provider,
        owner_provider_user_id=context.owner_provider_user_id,
    )
    if projection is None:
        raise _not_found()
    return projection


@router.get("/{trace_id}/events", response_model=TraceEventPage)
def catch_up_api_trace_events(
    trace_id: TraceId,
    response: Response,
    after_sequence: int = Query(default=-1, ge=-1),
    limit: int = Query(default=100, ge=1, le=250),
    context: TraceCaptureContext = Depends(require_api_trace_capture),
    db: Session = Depends(get_db),
) -> TraceEventPage:
    response.headers["Cache-Control"] = "private, no-store"
    events = get_trace_events(
        db,
        trace_id=trace_id,
        owner_provider=context.owner_provider,
        owner_provider_user_id=context.owner_provider_user_id,
        after_sequence=after_sequence,
        limit=limit,
    )
    if events is None:
        raise _not_found()
    next_cursor = events[-1].sequence if events else after_sequence
    return TraceEventPage(items=events, next_cursor=next_cursor)


@router.post(
    "/{trace_id}/events",
    response_model=TraceEventProjection,
    status_code=status.HTTP_201_CREATED,
)
def ingest_api_trace_event(
    trace_id: TraceId,
    event: ClientTraceEventInput,
    response: Response,
    context: TraceCaptureContext = Depends(require_api_trace_capture),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> TraceEventProjection:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        recorded = append_client_trace_event(
            db,
            trace_id=trace_id,
            owner_provider=context.owner_provider,
            owner_provider_user_id=context.owner_provider_user_id,
            event=event,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if recorded is None:
        raise _not_found()
    return recorded


def _sse(event: str, data: dict, *, event_id: int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.extend(
        (f"event: {event}", f"data: {json.dumps(data, separators=(',', ':'))}")
    )
    return "\n".join(lines) + "\n\n"


async def _trace_event_stream(
    *,
    request: Request,
    db: Session,
    trace_id: str,
    context: TraceCaptureContext,
    after_sequence: int,
    poll_seconds: float,
    keepalive_seconds: float,
):
    cursor = after_sequence
    last_write = time.monotonic()
    while not await request.is_disconnected():
        db.expire_all()
        events = get_trace_events(
            db,
            trace_id=trace_id,
            owner_provider=context.owner_provider,
            owner_provider_user_id=context.owner_provider_user_id,
            after_sequence=cursor,
            limit=100,
        )
        if events is None:
            yield _sse(
                "expired",
                {"trace_id": trace_id, "reason": "not_found_or_expired"},
            )
            return
        if events:
            for event in events:
                cursor = event.sequence
                yield _sse("trace_event", event.model_dump(mode="json"), event_id=cursor)
            last_write = time.monotonic()
            continue
        if time.monotonic() - last_write >= keepalive_seconds:
            yield ": keepalive\n\n"
            last_write = time.monotonic()
        await asyncio.sleep(poll_seconds)
    logger.debug(
        "API trace stream closed",
        extra={
            "api_trace_stream_close_reason": "client_disconnect",
            "api_trace_surface": context.surface,
        },
    )


@router.get("/{trace_id}/stream")
def stream_api_trace_events(
    trace_id: TraceId,
    request: Request,
    after_sequence: int = Query(default=-1, ge=-1),
    context: TraceCaptureContext = Depends(require_api_trace_capture),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    if any(key.casefold() in _QUERY_CREDENTIALS for key in request.query_params):
        raise HTTPException(
            status_code=400,
            detail="Bearer credentials are not accepted in stream URLs.",
        )
    if get_trace_events(
        db,
        trace_id=trace_id,
        owner_provider=context.owner_provider,
        owner_provider_user_id=context.owner_provider_user_id,
        after_sequence=after_sequence,
        limit=1,
    ) is None:
        raise _not_found()
    return StreamingResponse(
        _trace_event_stream(
            request=request,
            db=db,
            trace_id=trace_id,
            context=context,
            after_sequence=after_sequence,
            poll_seconds=max(0.05, settings.api_trace_stream_poll_seconds),
            keepalive_seconds=max(0.1, settings.api_trace_stream_keepalive_seconds),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{trace_id}/export")
def export_api_trace(
    trace_id: TraceId,
    context: TraceCaptureContext = Depends(require_api_trace_capture),
    db: Session = Depends(get_db),
) -> JSONResponse:
    projection = get_trace_projection(
        db,
        trace_id=trace_id,
        owner_provider=context.owner_provider,
        owner_provider_user_id=context.owner_provider_user_id,
    )
    if projection is None:
        raise _not_found()
    safe_trace_id = re.sub(r"[^A-Za-z0-9_-]", "_", trace_id)[:64]
    return JSONResponse(
        projection.model_dump(mode="json"),
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="api-trace-{safe_trace_id}.json"',
        },
    )
