from __future__ import annotations

from types import SimpleNamespace

from app.config import Settings
from app.services.content_moderation import ContentModerationService


def _result(*, flagged: bool, categories=None, scores=None):
    return SimpleNamespace(
        flagged=flagged,
        categories=categories or {"violence": flagged, "harassment": False},
        category_scores=scores or {"violence": 0.92 if flagged else 0.001},
    )


def _response(result, *, request_id: str = "req_mod_123", model: str = "omni-moderation-latest"):
    return SimpleNamespace(
        _request_id=request_id,
        model=model,
        results=[result],
    )


class _Moderations:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class _Client:
    def __init__(self, response=None, error=None):
        self.moderations = _Moderations(response=response, error=error)


def _settings(**overrides):
    values = {
        "openai_api_key": "test-key",
        "content_moderation_model": "omni-moderation-test",
        "content_moderation_timeout_seconds": 3.5,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_content_moderation_allows_clean_provider_results():
    client = _Client(_response(_result(flagged=False)))
    service = ContentModerationService(_settings(), client)

    decision = service.moderate_text("friendly review", surface="review")

    assert decision.allowed is True
    assert decision.outcome == "allowed"
    assert decision.flagged is False
    assert decision.categories == {"violence": False, "harassment": False}
    assert decision.provider_request_id == "req_mod_123"
    assert client.moderations.calls[0]["model"] == "omni-moderation-test"
    assert client.moderations.calls[0]["input"] == "friendly review"
    assert client.moderations.calls[0]["timeout"] == 3.5
    assert client.moderations.calls[0]["extra_headers"]["X-Client-Request-Id"].startswith(
        "sh-moderation-"
    )


def test_content_moderation_flags_provider_flagged_results():
    service = ContentModerationService(_settings(), _Client(_response(_result(flagged=True))))

    decision = service.moderate_text("unsafe review", surface="chat_input")

    assert decision.allowed is False
    assert decision.outcome == "flagged"
    assert decision.flagged is True
    assert decision.categories["violence"] is True


def test_content_moderation_unavailable_for_missing_credentials_without_client():
    service = ContentModerationService(_settings(openai_api_key=None), client=None)

    decision = service.moderate_text("text", surface="chat_output")

    assert decision.allowed is False
    assert decision.outcome == "unavailable"
    assert decision.flagged is True
    assert decision.error_class == "missing_credentials"


def test_content_moderation_unavailable_for_timeouts_and_provider_failures():
    timeout_decision = ContentModerationService(
        _settings(),
        _Client(error=TimeoutError("slow provider")),
    ).moderate_text("text", surface="review")
    failed_decision = ContentModerationService(
        _settings(),
        _Client(error=RuntimeError("provider unavailable")),
    ).moderate_text("text", surface="review")

    assert timeout_decision.outcome == "unavailable"
    assert timeout_decision.error_class == "TimeoutError"
    assert failed_decision.outcome == "unavailable"
    assert failed_decision.error_class == "RuntimeError"


def test_content_moderation_unavailable_for_empty_or_malformed_results():
    empty_decision = ContentModerationService(
        _settings(),
        _Client(SimpleNamespace(results=[])),
    ).moderate_text("text", surface="review")
    malformed_decision = ContentModerationService(
        _settings(),
        _Client(_response(SimpleNamespace(categories={}, category_scores={}))),
    ).moderate_text("text", surface="review")
    missing_categories_decision = ContentModerationService(
        _settings(),
        _Client(_response(SimpleNamespace(flagged=False, category_scores={}))),
    ).moderate_text("text", surface="review")

    assert empty_decision.outcome == "unavailable"
    assert empty_decision.error_class == "ValueError"
    assert malformed_decision.outcome == "unavailable"
    assert malformed_decision.error_class == "ValueError"
    assert missing_categories_decision.outcome == "unavailable"


def test_content_moderation_metadata_excludes_raw_input_text():
    service = ContentModerationService(_settings(), _Client(_response(_result(flagged=True))))

    metadata = service.moderate_text("blocked private body", surface="review").metadata()

    assert metadata["provider"] == "openai"
    assert metadata["capability"] == "moderation"
    assert metadata["surface"] == "review"
    assert metadata["outcome"] == "flagged"
    assert metadata["categories"] == {"violence": True, "harassment": False}
    assert metadata["provider_request_id"] == "req_mod_123"
    assert "blocked private body" not in str(metadata)
    assert "input" not in metadata
