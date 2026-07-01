from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import get_settings
from app.models import ChatTurn
from app.services.chat.realtime import (
    SHOPPER_REALTIME_TOOL_NAME,
    ShopperRealtimeService,
    ShopperRealtimeToolCallRequest,
    shopper_realtime_client_request_id,
)
from app.routers.chat import get_shopper_realtime_service
from tests.test_chat_api import _chat_client, _counting_catalog_evaluator


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
def _voice_client(monkeypatch, *, secrets: FakeClientSecrets | None = None):
    with _chat_client(monkeypatch) as (client, sessions):
        settings = get_settings()
        settings.openai_api_key = "server-key-never-returned"
        settings.shopper_realtime_enabled = True
        settings.shopper_realtime_safety_identifier_secret = "shopper-secret"
        if secrets is not None:
            client.app.dependency_overrides[get_shopper_realtime_service] = lambda: (
                ShopperRealtimeService(settings, _fake_client(secrets))
            )
        try:
            yield client, sessions, settings
        finally:
            client.app.dependency_overrides.pop(get_shopper_realtime_service, None)


def _tool_call_payload(**overrides):
    payload = {
        "session_id": "shopper_realtime_session_1",
        "call_id": "call_voice_1",
        "name": SHOPPER_REALTIME_TOOL_NAME,
        "arguments": {"message": "do you have a moisturizer under $150"},
        "context": {"route": "/", "store_id": "1001"},
    }
    payload.update(overrides)
    return payload


def test_shopper_realtime_capability_reports_safe_unconfigured_reasons(monkeypatch):
    with _chat_client(monkeypatch) as (client, _):
        response = client.get("/api/chat/realtime/capability")

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "reason": "feature_disabled",
        "model": None,
        "webrtc_url": "https://api.openai.com/v1/realtime/calls",
        "tool_names": [],
    }
    assert "server-key" not in response.text


def test_shopper_realtime_session_api_returns_browser_secret_and_only_shopper_tool(monkeypatch):
    secrets = FakeClientSecrets(
        SimpleNamespace(value="ek_browser_only", expires_at=1_782_000_600)
    )

    with _voice_client(monkeypatch, secrets=secrets) as (client, _, _):
        response = client.post(
            "/api/chat/realtime/sessions",
            json={
                "context": {
                    "route": "/products/prod_5",
                    "store_id": "1001",
                    "product_id": "prod_5",
                }
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert body["client_secret"] == "ek_browser_only"
    assert body["model"] == "gpt-realtime-2"
    assert body["tool_names"] == [SHOPPER_REALTIME_TOOL_NAME]
    assert body["session_id"].startswith("shopper_realtime_")
    assert "server-key-never-returned" not in response.text

    call = secrets.calls[0]
    assert call["expires_after"] == {"anchor": "created_at", "seconds": 600}
    assert call["session"]["type"] == "realtime"
    assert call["session"]["model"] == "gpt-realtime-2"
    assert call["session"]["audio"]["input"]["transcription"]["model"] == (
        "gpt-4o-mini-transcribe"
    )
    assert [tool["name"] for tool in call["session"]["tools"]] == [
        SHOPPER_REALTIME_TOOL_NAME
    ]
    serialized_call = repr(call)
    assert "catalog_workflow" not in serialized_call
    assert "create_catalog_draft" not in serialized_call
    assert "refine_catalog_draft" not in serialized_call
    assert call["extra_headers"]["OpenAI-Safety-Identifier"]
    assert call["extra_headers"]["X-Client-Request-Id"].startswith(
        "sh-shopperrealtime-"
    )
    assert "server-key-never-returned" not in serialized_call


def test_shopper_realtime_session_rejects_missing_config_before_provider_call(monkeypatch):
    class RejectProviderUse:
        def create(self, **_kwargs):
            raise AssertionError("Realtime provider must not be called when disabled")

    secrets = FakeClientSecrets()
    fake_client = SimpleNamespace(
        realtime=SimpleNamespace(client_secrets=RejectProviderUse())
    )

    with _chat_client(monkeypatch) as (client, _):
        settings = get_settings()
        settings.openai_api_key = "server-key"
        settings.shopper_realtime_enabled = False
        client.app.dependency_overrides[get_shopper_realtime_service] = lambda: (
            ShopperRealtimeService(settings, fake_client)
        )
        response = client.post("/api/chat/realtime/sessions", json={})

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "realtime_disabled",
            "message": "The shopper Realtime capability is disabled.",
            "retryable": False,
        }
    }
    assert secrets.calls == []


def test_shopper_realtime_tool_call_reuses_chat_turn_semantics_and_idempotency(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.chat.orchestrator.evaluate_chat",
        _counting_catalog_evaluator(calls),
    )

    with _voice_client(monkeypatch) as (client, sessions, _):
        first_response = client.post(
            "/api/chat/realtime/tool-calls",
            json=_tool_call_payload(),
        )
        first = first_response.json()
        replay_response = client.post(
            "/api/chat/realtime/tool-calls",
            json=_tool_call_payload(conversation_id=first["chat_response"]["conversation_id"]),
        )
        replay = replay_response.json()

        with sessions() as session:
            turns = session.scalars(select(ChatTurn).order_by(ChatTurn.created_at)).all()

    expected_client_request_id = shopper_realtime_client_request_id(
        session_id="shopper_realtime_session_1",
        call_id="call_voice_1",
    )
    assert first_response.status_code == 200
    assert replay_response.status_code == 200
    assert calls == ["do you have a moisturizer under $150"]
    assert first["status"] == "succeeded"
    assert first["message"] == first["chat_response"]["message"]
    assert first["tool_output"]["conversation_id"] == first["chat_response"]["conversation_id"]
    assert first["tool_output"]["card_count"] == len(first["chat_response"]["cards"])
    assert first["chat_response"]["client_request_id"] == expected_client_request_id
    assert replay["chat_response"]["duplicate_replay"] is True
    assert replay["chat_response"]["turn_id"] == first["chat_response"]["turn_id"]
    assert len(turns) == 1
    assert turns[0].client_request_id == expected_client_request_id


def test_shopper_realtime_tool_call_rejects_disabled_feature_before_chat(monkeypatch):
    def fail_evaluate_chat(*_args, **_kwargs):
        raise AssertionError("Disabled shopper Realtime must not execute chat")

    monkeypatch.setattr("app.services.chat.orchestrator.evaluate_chat", fail_evaluate_chat)

    with _chat_client(monkeypatch) as (client, _):
        response = client.post(
            "/api/chat/realtime/tool-calls",
            json=_tool_call_payload(),
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "realtime_disabled",
            "message": "The shopper Realtime capability is disabled.",
            "retryable": False,
        }
    }


def test_shopper_realtime_tool_call_contract_rejects_admin_or_unknown_tools():
    with pytest.raises(ValidationError):
        ShopperRealtimeToolCallRequest.model_validate(
            _tool_call_payload(name="create_catalog_draft")
        )
