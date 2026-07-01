from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.services.auth.admin import bind_chat_trace_capture
from app.services.auth.clerk import ChatIdentity, optional_chat_identity
from app.services.chat.orchestrator import handle_chat
from app.services.chat.realtime import (
    ShopperRealtimeCapabilityResponse,
    ShopperRealtimeError,
    ShopperRealtimeService,
    ShopperRealtimeSessionRequest,
    ShopperRealtimeSessionResponse,
    ShopperRealtimeToolCallRequest,
    ShopperRealtimeToolCallResponse,
    shopper_realtime_tool_output,
)
from app.services.chat.schemas import ChatRequest, ChatResponse


router = APIRouter(prefix="/api", tags=["chat"])


def get_shopper_realtime_service(
    settings: Settings = Depends(get_settings),
) -> ShopperRealtimeService:
    return ShopperRealtimeService(settings)


@router.post("/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    identity: ChatIdentity = Depends(optional_chat_identity),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    with bind_chat_trace_capture(request, identity=identity, settings=settings):
        return handle_chat(db, payload, identity)


@router.get(
    "/chat/realtime/capability",
    response_model=ShopperRealtimeCapabilityResponse,
    summary="Read shopper voice assistant Realtime availability",
)
def shopper_realtime_capability(
    service: ShopperRealtimeService = Depends(get_shopper_realtime_service),
) -> ShopperRealtimeCapabilityResponse:
    return service.capability()


@router.post(
    "/chat/realtime/sessions",
    response_model=ShopperRealtimeSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a shopper voice assistant Realtime session",
)
def create_shopper_realtime_session(
    response: Response,
    payload: ShopperRealtimeSessionRequest | None = None,
    identity: ChatIdentity = Depends(optional_chat_identity),
    service: ShopperRealtimeService = Depends(get_shopper_realtime_service),
) -> ShopperRealtimeSessionResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.create_session(
            identity=identity,
            context=payload.context if payload else None,
        )
    except ShopperRealtimeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": exc.detail,
                "retryable": exc.retryable,
            },
        ) from exc


@router.post(
    "/chat/realtime/tool-calls",
    response_model=ShopperRealtimeToolCallResponse,
    summary="Execute a shopper voice assistant tool call",
)
def execute_shopper_realtime_tool_call(
    payload: ShopperRealtimeToolCallRequest,
    request: Request,
    db: Session = Depends(get_db),
    identity: ChatIdentity = Depends(optional_chat_identity),
    settings: Settings = Depends(get_settings),
    service: ShopperRealtimeService = Depends(get_shopper_realtime_service),
) -> ShopperRealtimeToolCallResponse:
    if error := service.configuration_error():
        raise HTTPException(
            status_code=error.status_code,
            detail={
                "code": error.code,
                "message": error.detail,
                "retryable": error.retryable,
            },
        ) from error
    chat_request = payload.to_chat_request()
    with bind_chat_trace_capture(request, identity=identity, settings=settings):
        chat_response = handle_chat(db, chat_request, identity)
    tool_output = shopper_realtime_tool_output(chat_response)
    return ShopperRealtimeToolCallResponse(
        message=tool_output.message,
        chat_response=chat_response,
        tool_output=tool_output,
    )
