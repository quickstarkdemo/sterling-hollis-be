from __future__ import annotations

import hashlib
import hmac
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.workflow_schemas import WorkflowEventInput
from app.catalog.ai_schemas import CatalogAICommandRequest
from app.catalog.admin_schemas import product_draft_v3_from_snapshot
from app.config import Settings
from app.models import CatalogDraftRevision, CatalogWorkflow
from app.api_traces.adapters import (
    new_openai_client_request_id,
    openai_request_ids,
)
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_admin import draft_revision_version
from app.services.catalog_suggestions import validate_suggestion_target_path
from app.services.catalog_workflow import append_workflow_event


RealtimeDraftToolName = Literal["create_catalog_draft", "refine_catalog_draft"]
RealtimeV3ToolName = Literal[
    "read_product_summary",
    "read_catalog_summary",
    "read_inventory_status",
    "read_publish_readiness",
    "propose_product_field",
]
RealtimeToolName = RealtimeDraftToolName | RealtimeV3ToolName
REALTIME_WEBRTC_URL = "https://api.openai.com/v1/realtime/calls"

REALTIME_INSTRUCTIONS = """You are the voice interface for Sterling Hollis Catalog Studio.
Help the presenter create or refine the private product draft in the current workflow. Keep
spoken responses concise. Use only the provided catalog draft tool when the presenter asks for
a product change. Never claim that a product was published, archived, or changed until the tool
returns success. Do not request or repeat credentials, customer identity, or private data."""

_VOICE_FIELD_PATHS = {
    "/description",
    "/benefits",
    "/specifications",
    "/seo/title",
    "/seo/description",
    "/seo/keywords",
}


def _voice_field_target_is_allowed(path: str) -> bool:
    if path in _VOICE_FIELD_PATHS:
        return True
    parts = path.split("/")
    return (
        len(parts) == 4
        and parts[1] == "media"
        and bool(parts[2])
        and parts[3] == "alt_text"
    )


class CatalogRealtimeError(RuntimeError):
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


class CatalogRealtimeSessionResponse(BaseModel):
    client_secret: str
    expires_at: int = Field(ge=1)
    workflow_id: str
    model: str
    webrtc_url: str = REALTIME_WEBRTC_URL
    tool_names: list[RealtimeToolName]
    session_id: str | None = None


class CatalogRealtimeSessionContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: Literal["workbench", "field"]
    product_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=1, max_length=64)
    expected_draft_version: int = Field(ge=1)
    target_path: str | None = Field(default=None, max_length=255)
    query_scopes: list[Literal["product", "catalog", "inventory", "readiness"]] = Field(
        default_factory=lambda: ["product", "catalog", "inventory", "readiness"],
        max_length=4,
    )

    @model_validator(mode="after")
    def validate_mode(self):
        if self.mode == "field":
            if not self.target_path:
                raise ValueError("field mode requires target_path")
            validate_suggestion_target_path(self.target_path)
            if not _voice_field_target_is_allowed(self.target_path):
                raise ValueError("target_path is not eligible for field voice assistance")
        elif self.target_path is not None:
            raise ValueError("target_path is only valid for field mode")
        if self.mode == "workbench" and not self.query_scopes:
            raise ValueError("workbench mode requires at least one query scope")
        if len(self.query_scopes) != len(set(self.query_scopes)):
            raise ValueError("query_scopes must be unique")
        return self


class CatalogRealtimeV3ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str | None = Field(default=None, min_length=1, max_length=1000)
    instruction: str | None = Field(default=None, min_length=1, max_length=1000)


class CatalogRealtimeV3ToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=64)
    call_id: str = Field(min_length=1, max_length=128)
    name: RealtimeV3ToolName
    arguments: CatalogRealtimeV3ToolArguments

    @model_validator(mode="after")
    def validate_arguments(self):
        if self.name == "propose_product_field":
            if not self.arguments.instruction:
                raise ValueError("field proposals require an instruction")
            if self.arguments.question is not None:
                raise ValueError("field proposals do not accept a question")
        else:
            if not self.arguments.question:
                raise ValueError("read tools require a question")
            if self.arguments.instruction is not None:
                raise ValueError("read tools cannot propose a product change")
        return self


class CatalogRealtimeToolArguments(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    current_draft_id: str | None = Field(default=None, max_length=64)
    expected_draft_version: int = Field(ge=0)


class CatalogRealtimeToolCallRequest(BaseModel):
    call_id: str = Field(min_length=1, max_length=128)
    name: RealtimeDraftToolName
    arguments: CatalogRealtimeToolArguments

    @model_validator(mode="after")
    def validate_tool_state(self):
        if self.name == "create_catalog_draft":
            if (
                self.arguments.current_draft_id is not None
                or self.arguments.expected_draft_version != 0
            ):
                raise ValueError("create_catalog_draft requires a new-draft state")
        elif (
            self.arguments.current_draft_id is None
            or self.arguments.expected_draft_version < 1
        ):
            raise ValueError("refine_catalog_draft requires the current draft and version")
        return self

    def to_catalog_command(self) -> CatalogAICommandRequest:
        return CatalogAICommandRequest.model_validate(self.arguments.model_dump())


class CatalogRealtimeService:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client

    def create_session(
        self,
        db: Session,
        *,
        workflow_id: str,
        principal: AuthenticatedPrincipal,
        context: CatalogRealtimeSessionContextRequest | None = None,
    ) -> CatalogRealtimeSessionResponse:
        workflow = db.scalar(
            select(CatalogWorkflow).where(
                CatalogWorkflow.id == workflow_id,
                CatalogWorkflow.owner_provider == principal.provider,
                CatalogWorkflow.owner_provider_user_id == principal.provider_user_id,
            )
        )
        if workflow is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Catalog Studio catalog workflow not found.",
            )
        tool_name: RealtimeDraftToolName = (
            "refine_catalog_draft"
            if workflow.draft_revision_id
            else "create_catalog_draft"
        )
        expected_draft_version = 0
        if workflow.draft_revision_id:
            revision = db.get(CatalogDraftRevision, workflow.draft_revision_id)
            if revision is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="The catalog workflow's current draft is unavailable.",
                )
            expected_draft_version = draft_revision_version(db, revision)
        session_id = f"realtime_session_{uuid4().hex[:24]}" if context else None
        context_payload: dict[str, Any] | None = None
        tool_names: list[RealtimeToolName]
        if context is not None:
            context_payload = self._validated_v3_context(
                db,
                workflow=workflow,
                context=context,
                principal=principal,
            )
            tool_names = self._v3_tool_names(context)
        else:
            tool_names = [tool_name]
        client_request_id = new_openai_client_request_id("realtime")
        response_id: str | None = None
        provider_request_id: str | None = None
        try:
            provider = self._resolve_client()
            created = provider.realtime.client_secrets.create(
                expires_after={
                    "anchor": "created_at",
                    "seconds": self.settings.catalog_studio_realtime_client_secret_ttl_seconds,
                },
                session=(
                    self._v3_session_config(tool_names)
                    if context is not None
                    else self._session_config(
                        tool_name,
                        current_draft_id=workflow.draft_revision_id,
                        expected_draft_version=expected_draft_version,
                    )
                ),
                extra_headers={
                    "OpenAI-Safety-Identifier": self._safety_identifier(principal),
                    "X-Client-Request-Id": client_request_id,
                },
            )
            response_id, provider_request_id = openai_request_ids(created)
            secret = str(getattr(created, "value", "") or "")
            expires_at = int(getattr(created, "expires_at", 0) or 0)
            if not secret or expires_at <= 0:
                raise CatalogRealtimeError(
                    code="realtime_invalid_response",
                    detail="Realtime did not return a usable client credential.",
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    retryable=True,
                )
        except CatalogRealtimeError as exc:
            self._record_session_event(
                db,
                workflow_id=workflow_id,
                principal=principal,
                status_value="failed",
                error=exc,
                session_id=session_id,
                context=context_payload,
                client_request_id=client_request_id,
                response_id=response_id,
                provider_request_id=provider_request_id,
            )
            raise
        except Exception as exc:
            error = self._provider_failure(exc)
            self._record_session_event(
                db,
                workflow_id=workflow_id,
                principal=principal,
                status_value="failed",
                error=error,
                session_id=session_id,
                context=context_payload,
                client_request_id=client_request_id,
                response_id=response_id,
                provider_request_id=provider_request_id,
            )
            raise error from exc

        self._record_session_event(
            db,
            workflow_id=workflow_id,
            principal=principal,
            status_value="succeeded",
            tool_name=tool_name if context is None else None,
            tool_names=tool_names,
            session_id=session_id,
            expires_at=expires_at,
            context=context_payload,
            client_request_id=client_request_id,
            response_id=response_id,
            provider_request_id=provider_request_id,
        )
        return CatalogRealtimeSessionResponse(
            client_secret=secret,
            expires_at=expires_at,
            workflow_id=workflow_id,
            model=self.settings.catalog_studio_realtime_model,
            tool_names=tool_names,
            session_id=session_id,
        )

    def _validated_v3_context(
        self,
        db: Session,
        *,
        workflow: CatalogWorkflow,
        context: CatalogRealtimeSessionContextRequest,
        principal: AuthenticatedPrincipal,
    ) -> dict[str, Any]:
        revision = db.get(CatalogDraftRevision, context.draft_id)
        if (
            revision is None
            or revision.catalog_product_id != context.product_id
            or revision.created_by != principal.provider_user_id
        ):
            raise HTTPException(status_code=404, detail="Catalog draft revision not found.")
        if workflow.draft_revision_id not in {None, revision.id}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The catalog workflow is linked to another draft.",
            )
        actual_version = draft_revision_version(db, revision)
        if actual_version != context.expected_draft_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Expected catalog draft version {context.expected_draft_version}, "
                    f"but current version is {actual_version}."
                ),
            )
        if context.mode == "field" and context.target_path.startswith("/media/"):
            product = product_draft_v3_from_snapshot(revision.snapshot_json)
            media_id = context.target_path.split("/")[2]
            if all(item.media_id != media_id for item in product.media):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="The selected media target is not present in the current draft.",
                )
        workflow.draft_revision_id = revision.id
        return {
            "mode": context.mode,
            "product_id": context.product_id,
            "draft_id": revision.id,
            "expected_draft_version": actual_version,
            "target_path": context.target_path,
            "query_scopes": context.query_scopes if context.mode == "workbench" else [],
        }

    @staticmethod
    def _v3_tool_names(
        context: CatalogRealtimeSessionContextRequest,
    ) -> list[RealtimeToolName]:
        if context.mode == "field":
            return ["propose_product_field"]
        by_scope: dict[str, RealtimeV3ToolName] = {
            "product": "read_product_summary",
            "catalog": "read_catalog_summary",
            "inventory": "read_inventory_status",
            "readiness": "read_publish_readiness",
        }
        return [by_scope[scope] for scope in context.query_scopes]

    def _v3_session_config(
        self,
        tool_names: list[RealtimeToolName],
    ) -> dict[str, Any]:
        tools = []
        for name in tool_names:
            if name == "propose_product_field":
                properties: dict[str, Any] = {
                    "instruction": {
                        "type": "string",
                        "description": (
                            "A bounded dictation or refinement instruction. The application "
                            "selects and validates the target field outside model arguments."
                        ),
                    },
                }
                required = ["instruction"]
                description = "Stage a proposal for the field selected by the application."
            else:
                properties = {
                    "question": {
                        "type": "string",
                        "description": "The merchandiser's bounded catalog question.",
                    }
                }
                required = ["question"]
                description = {
                    "read_product_summary": "Read the active product summary.",
                    "read_catalog_summary": "Read a bounded catalog summary.",
                    "read_inventory_status": "Read store inventory for the active product.",
                    "read_publish_readiness": "Read deterministic publish readiness.",
                }[name]
            tools.append(
                {
                    "type": "function",
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": properties,
                        "required": required,
                    },
                }
            )
        return {
            "type": "realtime",
            "model": self.settings.catalog_studio_realtime_model,
            "instructions": (
                "You are the bounded voice assistant for Sterling Hollis Catalog Studio. "
                "Use only the provided tools. Read tools never change state. A field tool "
                "stages one proposal for merchant review and never saves, publishes, archives, "
                "or changes inventory. Do not request credentials or private customer data."
            ),
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "transcription": {
                        "model": self.settings.catalog_studio_realtime_transcription_model,
                    }
                }
            },
            "tools": tools,
            "tool_choice": "auto",
        }

    def _resolve_client(self) -> Any:
        if not self.settings.catalog_studio_realtime_enabled:
            raise CatalogRealtimeError(
                code="realtime_disabled",
                detail="The Realtime capability is disabled.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=False,
            )
        if not self.settings.openai_api_key:
            raise CatalogRealtimeError(
                code="realtime_unavailable",
                detail="The Realtime capability is not configured.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=False,
            )
        if not self.settings.catalog_studio_realtime_safety_identifier_secret:
            raise CatalogRealtimeError(
                code="realtime_safety_identifier_unavailable",
                detail="The Realtime safety identifier is not configured.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=False,
            )
        if self.client is not None:
            return self.client
        try:
            from openai import OpenAI

            return OpenAI(
                api_key=self.settings.openai_api_key,
                timeout=self.settings.catalog_studio_realtime_timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover - environment-specific constructor failure
            raise CatalogRealtimeError(
                code="realtime_unavailable",
                detail="The Realtime capability is unavailable.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=False,
            ) from exc

    def _safety_identifier(self, principal: AuthenticatedPrincipal) -> str:
        source = f"{principal.provider}:{principal.provider_user_id}".encode()
        secret = self.settings.catalog_studio_realtime_safety_identifier_secret.encode()
        return hmac.new(secret, source, hashlib.sha256).hexdigest()

    def _session_config(
        self,
        tool_name: RealtimeDraftToolName,
        *,
        current_draft_id: str | None = None,
        expected_draft_version: int = 0,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "The requested product draft change, without private data.",
                },
                "expected_draft_version": {
                    "type": "integer",
                    "const": expected_draft_version,
                },
            },
            "required": ["instruction", "expected_draft_version"],
        }
        if tool_name == "refine_catalog_draft":
            parameters["properties"]["current_draft_id"] = {
                "type": "string",
                "const": current_draft_id,
            }
            parameters["required"].append("current_draft_id")
        return {
            "type": "realtime",
            "model": self.settings.catalog_studio_realtime_model,
            "instructions": REALTIME_INSTRUCTIONS,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "transcription": {
                        "model": self.settings.catalog_studio_realtime_transcription_model,
                    }
                }
            },
            "tools": [
                {
                    "type": "function",
                    "name": tool_name,
                    "description": (
                        "Create the private product draft for this workflow."
                        if tool_name == "create_catalog_draft"
                        else "Refine the current private product draft for this workflow."
                    ),
                    "parameters": parameters,
                }
            ],
            "tool_choice": "auto",
        }

    def _record_session_event(
        self,
        db: Session,
        *,
        workflow_id: str,
        principal: AuthenticatedPrincipal,
        status_value: Literal["succeeded", "failed"],
        tool_name: RealtimeDraftToolName | None = None,
        tool_names: list[RealtimeToolName] | None = None,
        session_id: str | None = None,
        expires_at: int | None = None,
        context: dict[str, Any] | None = None,
        client_request_id: str | None = None,
        response_id: str | None = None,
        provider_request_id: str | None = None,
        error: CatalogRealtimeError | None = None,
    ) -> None:
        append_workflow_event(
            db,
            workflow_id=workflow_id,
            principal=principal,
            settings=self.settings,
            event=WorkflowEventInput(
                client_event_id=f"realtime-session-{uuid4().hex}",
                stage="voice",
                capability="realtime",
                status=status_value,
                business_summary=(
                    "Realtime voice is ready for this catalog workflow."
                    if status_value == "succeeded"
                    else (error.detail if error else "Realtime voice could not start.")
                ),
                model=self.settings.catalog_studio_realtime_model,
                request_id=provider_request_id or response_id,
                error_code=error.code if error else None,
                retryable=error.retryable if error else False,
                request_payload={
                    "client_request_id": client_request_id,
                    "input": {
                        "action": "create_realtime_session",
                        "safety_identifier_attached": bool(
                            self.settings.catalog_studio_realtime_enabled
                            and self.settings.openai_api_key
                            and self.settings.catalog_studio_realtime_safety_identifier_secret
                        ),
                    }
                },
                response_payload={
                    "status": "ready" if status_value == "succeeded" else "failed",
                    "response_id": response_id,
                    "provider_request_id": provider_request_id,
                    "tool_name": tool_name,
                    "tool_names": tool_names or ([tool_name] if tool_name else []),
                    "session_id": session_id,
                    "expires_at": expires_at,
                    "context": context,
                },
            ),
        )

    @staticmethod
    def _provider_failure(exc: Exception) -> CatalogRealtimeError:
        if isinstance(exc, TimeoutError) or type(exc).__name__ == "APITimeoutError":
            return CatalogRealtimeError(
                code="realtime_timeout",
                detail="Realtime timed out while creating a client credential.",
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                retryable=True,
            )
        if type(exc).__name__ in {"APIConnectionError", "ConnectError"}:
            return CatalogRealtimeError(
                code="realtime_unavailable",
                detail="Realtime is temporarily unavailable.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=True,
            )
        provider_status = getattr(exc, "status_code", None)
        return CatalogRealtimeError(
            code="realtime_failed",
            detail="Realtime could not create a client credential.",
            status_code=status.HTTP_502_BAD_GATEWAY,
            retryable=bool(provider_status == 429 or (provider_status and provider_status >= 500)),
        )


def reject_legacy_realtime_mutation_for_v3(
    db: Session,
    *,
    workflow_id: str,
    principal: AuthenticatedPrincipal,
) -> None:
    workflow = db.scalar(
        select(CatalogWorkflow).where(
            CatalogWorkflow.id == workflow_id,
            CatalogWorkflow.owner_provider == principal.provider,
            CatalogWorkflow.owner_provider_user_id == principal.provider_user_id,
        )
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Catalog Studio catalog workflow not found.")
    if not workflow.draft_revision_id:
        return
    revision = db.get(CatalogDraftRevision, workflow.draft_revision_id)
    if revision is not None and revision.snapshot_json.get("schema_version") == 3:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "V3 voice actions require a bounded Realtime context and a reviewable "
                "field suggestion."
            ),
        )


def record_realtime_tool_call(
    db: Session,
    *,
    workflow_id: str,
    request: CatalogRealtimeToolCallRequest,
    idempotency_key: str,
    principal: AuthenticatedPrincipal,
    settings: Settings,
) -> None:
    event_source = (
        f"{principal.provider}:{principal.provider_user_id}:{workflow_id}:"
        f"{idempotency_key.strip()}:{request.call_id}"
    )
    event_key = hashlib.sha256(event_source.encode()).hexdigest()[:32]
    append_workflow_event(
        db,
        workflow_id=workflow_id,
        principal=principal,
        settings=settings,
        event=WorkflowEventInput(
            client_event_id=f"realtime-tool-{event_key}",
            stage="voice",
            capability="realtime",
            status="succeeded",
            business_summary="The presenter requested a product draft change by voice.",
            model=settings.catalog_studio_realtime_model,
            draft_id=request.arguments.current_draft_id,
            request_payload={
                "input": {
                    "action": request.name,
                    "draft_id": request.arguments.current_draft_id,
                    "expected_draft_version": request.arguments.expected_draft_version,
                }
            },
            response_payload={"status": "accepted", "tool_name": request.name},
        ),
    )
