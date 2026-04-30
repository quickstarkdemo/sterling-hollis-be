from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.auth.clerk import ChatIdentity, optional_chat_identity
from app.services.chat.orchestrator import handle_chat
from app.services.chat.schemas import ChatRequest, ChatResponse


router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    db: Session = Depends(get_db),
    identity: ChatIdentity = Depends(optional_chat_identity),
) -> ChatResponse:
    return handle_chat(db, request, identity)
