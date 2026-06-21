from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal, Mapping

from app.api_traces.operations import new_openai_client_request_id, openai_request_ids
from app.config import Settings

ContentModerationOutcome = Literal["allowed", "flagged", "unavailable"]


@dataclass(frozen=True)
class ContentModerationDecision:
    outcome: ContentModerationOutcome
    flagged: bool
    model: str
    surface: str
    categories: dict[str, bool]
    category_scores: dict[str, float]
    provider_request_id: str | None
    client_request_id: str | None
    latency_ms: int
    error_class: str | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"

    def metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": "openai",
            "capability": "moderation",
            "model": self.model,
            "surface": self.surface,
            "outcome": self.outcome,
            "flagged": self.flagged,
            "categories": self.categories,
            "category_scores": self.category_scores,
            "latency_ms": self.latency_ms,
        }
        if self.provider_request_id:
            payload["provider_request_id"] = self.provider_request_id
        if self.client_request_id:
            payload["client_request_id"] = self.client_request_id
        if self.error_class:
            payload["error_class"] = self.error_class
        return payload


class ContentModerationService:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client

    def moderate_text(self, text: str, *, surface: str) -> ContentModerationDecision:
        model = str(self.settings.content_moderation_model or "omni-moderation-latest")
        started = monotonic()
        client_request_id = new_openai_client_request_id("moderation")
        if not str(self.settings.openai_api_key or "").strip() and self.client is None:
            return _unavailable(
                model=model,
                surface=surface,
                started=started,
                client_request_id=client_request_id,
                error_class="missing_credentials",
            )

        try:
            response = self._client().moderations.create(
                model=model,
                input=text,
                extra_headers={"X-Client-Request-Id": client_request_id},
                timeout=max(1.0, float(self.settings.content_moderation_timeout_seconds)),
            )
            _response_id, provider_request_id = openai_request_ids(response)
            result = _first_result(response)
            flagged = _bool_field(result, "flagged")
            if flagged is None:
                raise ValueError("missing_flagged")
            categories = _required_bool_mapping(getattr(result, "categories", None))
            category_scores = _required_score_mapping(getattr(result, "category_scores", None))
            return ContentModerationDecision(
                outcome="flagged" if flagged else "allowed",
                flagged=flagged,
                model=str(getattr(response, "model", None) or getattr(result, "model", None) or model),
                surface=surface,
                categories=categories,
                category_scores=category_scores,
                provider_request_id=provider_request_id,
                client_request_id=client_request_id,
                latency_ms=_elapsed_ms(started),
            )
        except TimeoutError:
            return _unavailable(
                model=model,
                surface=surface,
                started=started,
                client_request_id=client_request_id,
                error_class="TimeoutError",
            )
        except Exception as exc:
            return _unavailable(
                model=model,
                surface=surface,
                started=started,
                client_request_id=client_request_id,
                error_class=type(exc).__name__,
            )

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        from openai import OpenAI

        return OpenAI(api_key=self.settings.openai_api_key)


def _elapsed_ms(started: float) -> int:
    return max(0, int((monotonic() - started) * 1000))


def _unavailable(
    *,
    model: str,
    surface: str,
    started: float,
    client_request_id: str | None,
    error_class: str,
) -> ContentModerationDecision:
    return ContentModerationDecision(
        outcome="unavailable",
        flagged=True,
        model=model,
        surface=surface,
        categories={},
        category_scores={},
        provider_request_id=None,
        client_request_id=client_request_id,
        latency_ms=_elapsed_ms(started),
        error_class=error_class,
    )


def _first_result(response: Any) -> Any:
    results = getattr(response, "results", None)
    if not isinstance(results, list) or not results:
        raise ValueError("missing_results")
    return results[0]


def _bool_field(value: Any, field: str) -> bool | None:
    result = getattr(value, field, None)
    return result if isinstance(result, bool) else None


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, Mapping) else None
    if hasattr(value, "__dict__"):
        return vars(value)
    return None


def _required_bool_mapping(value: Any) -> dict[str, bool]:
    mapping = _as_mapping(value)
    if mapping is None:
        raise ValueError("missing_categories")
    result = {
        str(key): bool(flagged)
        for key, flagged in mapping.items()
        if isinstance(flagged, bool)
    }
    if not result and mapping:
        raise ValueError("malformed_categories")
    return result


def _required_score_mapping(value: Any) -> dict[str, float]:
    mapping = _as_mapping(value)
    if mapping is None:
        raise ValueError("missing_category_scores")
    result = {
        str(key): float(score)
        for key, score in mapping.items()
        if isinstance(score, int | float) and not isinstance(score, bool)
    }
    if not result and mapping:
        raise ValueError("malformed_category_scores")
    return result
