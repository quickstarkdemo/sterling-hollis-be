from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Mapping


REDACTED = "[REDACTED]"
RETENTION_MARKER = {"_retention": "expired"}

BUILT_IN_REDACTED_KEYS = frozenset(
    {
        "address",
        "api_key",
        "authorization",
        "cookie",
        "credentials",
        "customer",
        "customer_id",
        "email",
        "first_name",
        "full_name",
        "headers",
        "identity",
        "image_bytes",
        "instructions",
        "ip_address",
        "last_name",
        "password",
        "phone",
        "private_reasoning",
        "provider_user_id",
        "raw_audio",
        "reasoning",
        "secret",
        "system",
        "system_prompt",
        "token",
        "user_id",
    }
)

DEFAULT_ALLOWED_KEYS = frozenset(
    {
        "action",
        "action_count",
        "action_label",
        "action_summaries",
        "action_type",
        "agent_mode",
        "approval_status",
        "agent",
        "agent_name",
        "artifact",
        "artifact_id",
        "artifact_type",
        "attempt",
        "attributes",
        "auth_required",
        "availability",
        "base_version",
        "blocked",
        "brand",
        "capability",
        "capability_id",
        "capability_name",
        "capability_operation",
        "capability_side_effect",
        "candidate_count",
        "card_count",
        "card_summaries",
        "categories",
        "category",
        "category_scores",
        "citation_count",
        "citation_summaries",
        "citation_ids",
        "client_request_id",
        "color",
        "content_type",
        "conversation_id",
        "context",
        "decision",
        "description",
        "detail_urls",
        "draft",
        "draft_id",
        "draft_summary",
        "draft_version",
        "duplicate_replay",
        "endpoint",
        "error",
        "error_code",
        "evaluator_source",
        "event",
        "expires_at",
        "expected_draft_version",
        "flagged",
        "fallback",
        "fallback_reason",
        "gender",
        "host",
        "http_method",
        "history_count",
        "id",
        "image_direction",
        "image_job_id",
        "image_link",
        "image_set",
        "image_url",
        "input",
        "input_tokens",
        "cached_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
        "images_generated",
        "input_origin",
        "inventory",
        "inventory_qty",
        "identity_status",
        "intent",
        "items",
        "kind",
        "label",
        "link",
        "material",
        "match_count",
        "media_type",
        "metadata",
        "method",
        "model",
        "mode",
        "moderation",
        "mutation",
        "name",
        "objective_weight",
        "operation",
        "output",
        "output_tokens",
        "presenter_input",
        "price",
        "price_max",
        "price_min",
        "primary_url",
        "product",
        "product_id",
        "products",
        "provider",
        "provider_request_id",
        "published_product_id",
        "query_scopes",
        "request",
        "request_bytes",
        "request_id",
        "response_id",
        "response",
        "response_bytes",
        "result_count",
        "result",
        "retry_reason",
        "retryable",
        "route",
        "safety_identifier_attached",
        "season",
        "service",
        "session_id",
        "selected_agent",
        "selected_tool",
        "size",
        "size_bytes",
        "scope",
        "stage",
        "status",
        "status_code",
        "store_id",
        "strategy",
        "surface",
        "summary",
        "source_asset_count",
        "source_asset_ids",
        "suggestion_count",
        "suggestion_set_id",
        "reasoning_tokens",
        "target_path",
        "target_paths",
        "thumbnail_url",
        "title",
        "tool",
        "tool_name",
        "tool_names",
        "tool_trace_summary",
        "tool_count",
        "total_tokens",
        "tools",
        "unknown_count",
        "usage",
        "variant",
        "variant_id",
        "variant_index",
        "variants",
        "vector_dimension",
        "workflow_id",
        "turn_id",
        "trigger_type",
        "visible_created_at",
        "visible_message_id",
        "visible_messages",
        "visible_role",
        "visible_source",
        "visible_text",
    }
)

_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(
        r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        r"(?![A-Za-z0-9.-])"
    ),
    re.compile(r"(?<!\d)\+[1-9]\d{7,14}(?!\d)"),
)


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    max_depth: int = 6
    max_string_length: int = 1000
    max_array_length: int = 25
    max_object_keys: int = 50
    max_bytes: int = 16_384
    redacted_keys: frozenset[str] = BUILT_IN_REDACTED_KEYS
    allowed_keys: frozenset[str] = DEFAULT_ALLOWED_KEYS


def normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().casefold()).strip("_")


def configured_redacted_keys(value: str) -> frozenset[str]:
    configured = {normalized_key(key) for key in value.split(",") if key.strip()}
    return frozenset(BUILT_IN_REDACTED_KEYS | configured)


def key_is_redacted(key: str, redacted_keys: frozenset[str]) -> bool:
    if key in redacted_keys:
        return True
    return bool(set(key.split("_")) & {"authorization", "password", "secret", "token"})


def safe_observability_text(value: object, *, max_length: int) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    if len(text) <= max_length:
        return text
    omitted = len(text) - max_length
    marker = f"...[truncated {omitted} chars]"
    if len(marker) >= max_length:
        return marker[:max_length]
    return f"{text[: max_length - len(marker)]}{marker}"


def _sanitize_value(value: Any, *, policy: RedactionPolicy, depth: int) -> Any:
    if depth > policy.max_depth:
        return {"_truncated": "maximum depth reached"}
    if isinstance(value, float) and not math.isfinite(value):
        return REDACTED
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return safe_observability_text(value, max_length=max(1, policy.max_string_length))
    if isinstance(value, bytes | bytearray | memoryview):
        return REDACTED
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        omitted = 0
        truncated = 0
        allowed_count = 0
        max_keys = max(1, policy.max_object_keys)
        for raw_key in sorted(value, key=lambda item: str(item).casefold()):
            key = normalized_key(raw_key)
            if not key:
                omitted += 1
                continue
            if key_is_redacted(key, policy.redacted_keys):
                projected[key] = REDACTED
                continue
            if key not in policy.allowed_keys:
                omitted += 1
                continue
            if allowed_count >= max_keys:
                truncated += 1
                continue
            projected[key] = _sanitize_value(
                value[raw_key],
                policy=policy,
                depth=depth + 1,
            )
            allowed_count += 1
        if omitted:
            projected["_omitted_fields"] = omitted
        if truncated:
            projected["_truncated_fields"] = truncated
        return projected
    if isinstance(value, (list, tuple, set)):
        rows = sorted(value, key=repr) if isinstance(value, set) else list(value)
        limit = max(1, policy.max_array_length)
        projected = [
            _sanitize_value(row, policy=policy, depth=depth + 1)
            for row in rows[:limit]
        ]
        if len(rows) > limit:
            projected.append({"_truncated_items": len(rows) - limit})
        return projected
    return REDACTED


def sanitize_observability_payload(value: Any, *, policy: RedactionPolicy) -> dict:
    projected = _sanitize_value(value, policy=policy, depth=0)
    if not isinstance(projected, dict):
        projected = {"value": projected}
    return enforce_payload_bytes(projected, max_bytes=policy.max_bytes)


def enforce_payload_bytes(projected: dict, *, max_bytes: int) -> dict:
    encoded = json.dumps(
        projected,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    max_bytes = max(96, max_bytes)
    if len(encoded) > max_bytes:
        return {
            "_truncated": f"payload exceeded {max_bytes} bytes",
            "_projected_bytes": len(encoded),
        }
    return projected
