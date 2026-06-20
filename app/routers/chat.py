from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.services.auth.admin import bind_chat_trace_capture
from app.services.auth.clerk import ChatIdentity, optional_chat_identity
from app.services.chat.orchestrator import handle_chat
from app.services.chat.schemas import ChatRequest, ChatResponse


router = APIRouter(prefix="/api", tags=["chat"])


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
