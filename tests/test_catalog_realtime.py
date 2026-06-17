from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base
from app.models import CatalogWorkflowEvent
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_realtime import (
    CatalogRealtimeError,
    CatalogRealtimeService,
    CatalogRealtimeToolCallRequest,
)
from app.services.catalog_workflow import start_catalog_workflow


class FakeClientSecrets:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def _fake_client(client_secrets: FakeClientSecrets):
    return SimpleNamespace(realtime=SimpleNamespace(client_secrets=client_secrets))


@contextmanager
def _realtime_context():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    db = sessions()
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        openai_api_key="server-api-key",
        catalog_studio_realtime_enabled=True,
        catalog_studio_realtime_safety_identifier_secret="stable-test-secret",
    )
    principal = AuthenticatedPrincipal(
        provider="clerk",
        provider_user_id="user_admin",
        email="admin@example.com",
        claims={},
    )
    workflow = start_catalog_workflow(
        db,
        principal=principal,
        title="Voice-authored coat",
        business_summary="Preparing a private product draft.",
        settings=settings,
        idempotency_key="voice-workflow",
        now=datetime(2026, 6, 17, tzinfo=timezone.utc),
    )
    try:
        yield db, settings, principal, workflow.id
    finally:
        db.close()
        engine.dispose()


def test_session_issues_workflow_bound_ephemeral_credential_with_only_draft_tool():
    secrets = FakeClientSecrets(
        SimpleNamespace(value="ek_short_lived", expires_at=1_782_000_600)
    )

    with _realtime_context() as (db, settings, principal, workflow_id):
        result = CatalogRealtimeService(settings, _fake_client(secrets)).create_session(
            db,
            workflow_id=workflow_id,
            principal=principal,
        )
        events = db.scalars(
            select(CatalogWorkflowEvent).where(
                CatalogWorkflowEvent.workflow_id == workflow_id
            )
        ).all()

    assert result.client_secret == "ek_short_lived"
    assert result.expires_at == 1_782_000_600
    assert result.tool_names == ["create_catalog_draft"]
    call = secrets.calls[0]
    assert call["expires_after"] == {"anchor": "created_at", "seconds": 600}
    assert call["session"]["type"] == "realtime"
    assert call["session"]["model"] == "gpt-realtime-2"
    assert call["session"]["audio"]["input"]["transcription"]["model"] == (
        "gpt-4o-mini-transcribe"
    )
    assert [tool["name"] for tool in call["session"]["tools"]] == [
        "create_catalog_draft"
    ]
    assert call["session"]["tools"][0]["parameters"]["properties"][
        "expected_draft_version"
    ] == {"type": "integer", "const": 0}
    serialized_tools = repr(call["session"]["tools"])
    assert "publish_catalog" not in serialized_tools
    assert "archive_catalog" not in serialized_tools
    expected_safety_id = hmac.new(
        b"stable-test-secret", b"clerk:user_admin", hashlib.sha256
    ).hexdigest()
    assert call["extra_headers"] == {"OpenAI-Safety-Identifier": expected_safety_id}
    assert "user_admin" not in expected_safety_id
    assert events[-1].capability == "realtime"
    assert events[-1].request_json == {
        "input": {
            "action": "create_realtime_session",
            "safety_identifier_attached": True,
        }
    }
    assert "audio" not in repr(events[-1].request_json).lower()
    assert "ek_short_lived" not in repr(events[-1].response_json)


def test_session_rejects_workflows_owned_by_another_administrator_before_provider_call():
    secrets = FakeClientSecrets(
        SimpleNamespace(value="ek_should_not_be_used", expires_at=1_782_000_600)
    )

    with _realtime_context() as (db, settings, _principal, workflow_id):
        settings.catalog_studio_shared_workflows = True
        other = AuthenticatedPrincipal(provider="clerk", provider_user_id="another_admin")
        with pytest.raises(HTTPException) as exc_info:
            CatalogRealtimeService(settings, _fake_client(secrets)).create_session(
                db,
                workflow_id=workflow_id,
                principal=other,
            )

    assert exc_info.value.status_code == 404
    assert secrets.calls == []


def test_session_provider_outage_is_retryable_and_does_not_expose_server_key():
    provider_error = type("APIConnectionError", (Exception,), {})()
    secrets = FakeClientSecrets(error=provider_error)

    with _realtime_context() as (db, settings, principal, workflow_id):
        with pytest.raises(CatalogRealtimeError) as exc_info:
            CatalogRealtimeService(settings, _fake_client(secrets)).create_session(
                db,
                workflow_id=workflow_id,
                principal=principal,
            )
        event = db.scalars(
            select(CatalogWorkflowEvent).where(
                CatalogWorkflowEvent.workflow_id == workflow_id
            )
        ).all()[-1]

    assert exc_info.value.code == "realtime_unavailable"
    assert exc_info.value.retryable is True
    assert exc_info.value.status_code == 503
    assert "server-api-key" not in repr(event.request_json)
    assert "server-api-key" not in repr(event.response_json)
    assert event.retryable is True


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "call_id": "call-create-invalid",
                "name": "create_catalog_draft",
                "arguments": {
                    "instruction": "Create a coat.",
                    "current_draft_id": "draft_existing",
                    "expected_draft_version": 1,
                },
            },
            "create_catalog_draft requires a new-draft state",
        ),
        (
            {
                "call_id": "call-refine-invalid",
                "name": "refine_catalog_draft",
                "arguments": {
                    "instruction": "Make it ivory.",
                    "expected_draft_version": 0,
                },
            },
            "refine_catalog_draft requires the current draft and version",
        ),
    ],
)
def test_tool_call_contract_rejects_invalid_draft_state(payload, message):
    with pytest.raises(ValidationError, match=message):
        CatalogRealtimeToolCallRequest.model_validate(payload)


def test_tool_call_contract_rejects_unapproved_operations():
    with pytest.raises(ValidationError):
        CatalogRealtimeToolCallRequest.model_validate(
            {
                "call_id": "call-publish",
                "name": "publish_catalog_product",
                "arguments": {
                    "instruction": "Publish it.",
                    "current_draft_id": "draft_existing",
                    "expected_draft_version": 1,
                },
            }
        )


def test_refinement_session_pins_the_current_draft_and_version():
    settings = Settings(
        _env_file=None,
        catalog_studio_realtime_safety_identifier_secret="test-secret",
    )

    session = CatalogRealtimeService(settings)._session_config(
        "refine_catalog_draft",
        current_draft_id="draft_current",
        expected_draft_version=3,
    )

    tool = session["tools"][0]
    assert tool["name"] == "refine_catalog_draft"
    assert tool["parameters"]["properties"]["current_draft_id"] == {
        "type": "string",
        "const": "draft_current",
    }
    assert tool["parameters"]["properties"]["expected_draft_version"] == {
        "type": "integer",
        "const": 3,
    }
