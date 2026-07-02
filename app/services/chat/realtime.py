from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Literal
from uuid import uuid4

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field

from app.api_traces.adapters import (
    new_openai_client_request_id,
)
from app.config import Settings
from app.services.auth.clerk import ChatIdentity
from app.services.chat.schemas import ChatAction, ChatContext, ChatRequest, ChatResponse


ShopperRealtimeToolName = Literal["shopper_chat_turn"]
SHOPPER_REALTIME_WEBRTC_URL = "https://api.openai.com/v1/realtime/calls"
SHOPPER_REALTIME_TOOL_NAME: ShopperRealtimeToolName = "shopper_chat_turn"
PRODUCTION_ENVIRONMENTS = {"prod", "production"}

SHOPPER_REALTIME_INSTRUCTIONS = """You are the consumer-facing voice assistant for Sterling Hollis.
Use the shopper_chat_turn tool for product, availability, store, style, account, order, and recommendation
requests. Keep spoken responses concise and grounded in the tool result. Never request payment credentials,
passwords, API keys, or private customer identifiers. Do not mention internal routes, admin workflows, system
configuration, raw tool payloads, or provider details."""


class ShopperRealtimeError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        detail: str,
        status_code: int,
        retryable: bool,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.retryable = retryable


class ShopperRealtimeCapabilityResponse(BaseModel):
    configured: bool
    reason: Literal[
        "feature_disabled",
        "openai_unconfigured",
        "safety_identifier_unconfigured",
    ] | None = None
    model: str | None = None
    webrtc_url: str = SHOPPER_REALTIME_WEBRTC_URL
    tool_names: list[ShopperRealtimeToolName] = Field(default_factory=list)


class ShopperRealtimeSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: ChatContext = Field(default_factory=ChatContext)


class ShopperRealtimeSessionResponse(BaseModel):
    client_secret: str
    expires_at: int = Field(ge=1)
    model: str
    webrtc_url: str = SHOPPER_REALTIME_WEBRTC_URL
    tool_names: list[ShopperRealtimeToolName]
    session_id: str


class ShopperRealtimeToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=2000)


class ShopperRealtimeToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=96)
    call_id: str = Field(min_length=1, max_length=128)
    name: ShopperRealtimeToolName
    arguments: ShopperRealtimeToolArguments
    conversation_id: str | None = Field(default=None, max_length=64)
    context: ChatContext = Field(default_factory=ChatContext)

    def to_chat_request(self) -> ChatRequest:
        return ChatRequest(
            message=self.arguments.message,
            conversation_id=self.conversation_id,
            client_request_id=shopper_realtime_client_request_id(
                session_id=self.session_id,
                call_id=self.call_id,
            ),
            trigger_type="user_submit",
            context=self.context,
        )


class ShopperRealtimeToolOutput(BaseModel):
    message: str
    conversation_id: str
    turn_id: str | None = None
    requires_followup: bool = False
    clarifying_question: str | None = None
    card_count: int = 0
    actions: list[ChatAction] = Field(default_factory=list)
    selected_tool: str | None = None
    capability_id: str | None = None


class ShopperRealtimeToolCallResponse(BaseModel):
    status: Literal["succeeded"] = "succeeded"
    retryable: bool = False
    message: str
    chat_response: ChatResponse
    tool_output: ShopperRealtimeToolOutput


def shopper_realtime_client_request_id(*, session_id: str, call_id: str) -> str:
    source = f"{session_id}:{call_id}".encode()
    digest = hashlib.sha256(source).hexdigest()[:40]
    return f"voice_{digest}"


class ShopperRealtimeService:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client

    def capability(self) -> ShopperRealtimeCapabilityResponse:
        error = self.configuration_error()
        if error:
            reason: Literal[
                "feature_disabled",
                "openai_unconfigured",
                "safety_identifier_unconfigured",
            ]
            if error.code == "realtime_disabled":
                reason = "feature_disabled"
            elif error.code == "realtime_safety_identifier_unavailable":
                reason = "safety_identifier_unconfigured"
            else:
                reason = "openai_unconfigured"
            return ShopperRealtimeCapabilityResponse(
                configured=False,
                reason=reason,
            )
        return ShopperRealtimeCapabilityResponse(
            configured=True,
            model=self.settings.shopper_realtime_model,
            tool_names=[SHOPPER_REALTIME_TOOL_NAME],
        )

    def configuration_error(self) -> ShopperRealtimeError | None:
        if not self._shopper_realtime_enabled():
            return ShopperRealtimeError(
                code="realtime_disabled",
                detail="The shopper Realtime capability is disabled.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=False,
            )
        if not self.settings.openai_api_key:
            return ShopperRealtimeError(
                code="realtime_unavailable",
                detail="The shopper Realtime capability is not configured.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=False,
            )
        if not self._safety_secret():
            return ShopperRealtimeError(
                code="realtime_safety_identifier_unavailable",
                detail="The shopper Realtime safety identifier is not configured.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=False,
            )
        return None

    def _shopper_realtime_enabled(self) -> bool:
        if self.settings.shopper_realtime_enabled:
            return True
        if os.environ.get("SHOPPER_REALTIME_ENABLED") is not None:
            return False
        return self.settings.environment.strip().lower() in PRODUCTION_ENVIRONMENTS

    def create_session(
        self,
        *,
        identity: ChatIdentity,
        context: ChatContext | None = None,
    ) -> ShopperRealtimeSessionResponse:
        session_id = f"shopper_realtime_{uuid4().hex[:24]}"
        client_request_id = new_openai_client_request_id("shopper-realtime")
        try:
            provider = self._resolve_client()
            created = provider.realtime.client_secrets.create(
                expires_after={
                    "anchor": "created_at",
                    "seconds": self.settings.shopper_realtime_client_secret_ttl_seconds,
                },
                session=self._session_config(context or ChatContext()),
                extra_headers={
                    "OpenAI-Safety-Identifier": self._safety_identifier(
                        identity,
                        session_id=session_id,
                    ),
                    "X-Client-Request-Id": client_request_id,
                },
            )
            secret = str(getattr(created, "value", "") or "")
            expires_at = int(getattr(created, "expires_at", 0) or 0)
            if not secret or expires_at <= 0:
                raise ShopperRealtimeError(
                    code="realtime_invalid_response",
                    detail="Realtime did not return a usable client credential.",
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    retryable=True,
                )
        except ShopperRealtimeError:
            raise
        except Exception as exc:
            raise self._provider_failure(exc) from exc

        return ShopperRealtimeSessionResponse(
            client_secret=secret,
            expires_at=expires_at,
            model=self.settings.shopper_realtime_model,
            tool_names=[SHOPPER_REALTIME_TOOL_NAME],
            session_id=session_id,
        )

    def _session_config(self, context: ChatContext) -> dict[str, Any]:
        context_hint = self._context_hint(context)
        return {
            "type": "realtime",
            "model": self.settings.shopper_realtime_model,
            "instructions": (
                SHOPPER_REALTIME_INSTRUCTIONS
                if not context_hint
                else f"{SHOPPER_REALTIME_INSTRUCTIONS}\nCurrent storefront context: {context_hint}"
            ),
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "transcription": {
                        "model": self.settings.shopper_realtime_transcription_model,
                    }
                }
            },
            "tools": [
                {
                    "type": "function",
                    "name": SHOPPER_REALTIME_TOOL_NAME,
                    "description": (
                        "Submit the shopper's spoken storefront request to the Sterling "
                        "Hollis chat backend and return the visible assistant answer."
                    ),
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "The shopper's request in their own words.",
                            },
                        },
                        "required": ["message"],
                    },
                }
            ],
            "tool_choice": "auto",
        }

    @staticmethod
    def _context_hint(context: ChatContext) -> str | None:
        parts = []
        if context.route:
            parts.append(f"route={context.route}")
        if context.page_type:
            parts.append(f"page_type={context.page_type}")
        if context.store_id:
            parts.append(f"store_id={context.store_id}")
        if context.product_id:
            parts.append(f"product_id={context.product_id}")
        if context.category:
            parts.append(f"category={context.category}")
        if context.current_product:
            product_parts = [context.current_product.id]
            if context.current_product.title:
                product_parts.append(context.current_product.title)
            if context.current_product.category:
                product_parts.append(context.current_product.category)
            parts.append(f"current_product={' | '.join(product_parts)}")
        return "; ".join(parts) or None

    def _resolve_client(self) -> Any:
        if error := self.configuration_error():
            raise error
        if self.client is not None:
            return self.client
        try:
            from openai import OpenAI

            return OpenAI(
                api_key=self.settings.openai_api_key,
                timeout=self.settings.shopper_realtime_timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover - environment-specific constructor failure
            raise ShopperRealtimeError(
                code="realtime_unavailable",
                detail="The shopper Realtime capability is unavailable.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=False,
            ) from exc

    def _safety_secret(self) -> str:
        return (
            self.settings.shopper_realtime_safety_identifier_secret
            or self.settings.catalog_studio_realtime_safety_identifier_secret
        )

    def _safety_identifier(self, identity: ChatIdentity, *, session_id: str) -> str:
        if identity.principal:
            source = f"principal:{identity.principal.provider}:{identity.principal.provider_user_id}"
        elif identity.customer_id:
            source = f"customer:{identity.customer_id}"
        else:
            source = f"anonymous:{session_id}"
        return hmac.new(self._safety_secret().encode(), source.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _provider_failure(exc: Exception) -> ShopperRealtimeError:
        if isinstance(exc, TimeoutError) or type(exc).__name__ == "APITimeoutError":
            return ShopperRealtimeError(
                code="realtime_timeout",
                detail="Realtime timed out while creating a client credential.",
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                retryable=True,
            )
        if type(exc).__name__ in {"APIConnectionError", "ConnectError"}:
            return ShopperRealtimeError(
                code="realtime_unavailable",
                detail="Realtime is temporarily unavailable.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=True,
            )
        provider_status = getattr(exc, "status_code", None)
        return ShopperRealtimeError(
            code="realtime_failed",
            detail="Realtime could not create a client credential.",
            status_code=status.HTTP_502_BAD_GATEWAY,
            retryable=bool(provider_status == 429 or (provider_status and provider_status >= 500)),
        )


def shopper_realtime_tool_output(response: ChatResponse) -> ShopperRealtimeToolOutput:
    visible_message = response.clarifying_question if response.requires_followup and response.clarifying_question else response.message
    return ShopperRealtimeToolOutput(
        message=visible_message,
        conversation_id=response.conversation_id,
        turn_id=response.turn_id,
        requires_followup=response.requires_followup,
        clarifying_question=response.clarifying_question,
        card_count=len(response.cards),
        actions=response.actions,
        selected_tool=response.selected_tool,
        capability_id=response.capability_id,
    )
