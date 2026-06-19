from __future__ import annotations

import json

from app.observability.redaction import (
    RedactionPolicy,
    configured_redacted_keys,
    sanitize_observability_payload,
)


def test_shared_redaction_is_allowlist_first_and_recursive():
    policy = RedactionPolicy(
        redacted_keys=configured_redacted_keys("vendor_private"),
        max_string_length=80,
    )
    projected = sanitize_observability_payload(
        {
            "request": {
                "method": "POST",
                "route": "/v1/responses",
                "headers": {"Authorization": "Bearer top-secret"},
                "customer": {"email": "private@example.com"},
                "raw_audio": b"voice-bytes",
                "image_bytes": "base64-private",
                "private_reasoning": "hidden chain",
                "vendor_private": "configured secret",
                "unknown_payload": "must not persist",
                "description": "Contact private@example.com or +15551234567",
            }
        },
        policy=policy,
    )
    encoded = json.dumps(projected, sort_keys=True)

    assert projected["request"]["method"] == "POST"
    assert projected["request"]["route"] == "/v1/responses"
    assert projected["request"]["_omitted_fields"] == 1
    for secret in (
        "top-secret",
        "private@example.com",
        "voice-bytes",
        "base64-private",
        "hidden chain",
        "configured secret",
        "must not persist",
        "private@example.com",
        "+15551234567",
    ):
        assert secret not in encoded
    assert "[REDACTED]" in encoded


def test_shared_redaction_bounds_depth_cardinality_strings_and_total_bytes():
    policy = RedactionPolicy(
        max_depth=2,
        max_string_length=16,
        max_array_length=2,
        max_object_keys=2,
        max_bytes=220,
    )
    raw = {
        "request": {
            "title": "a title that is intentionally much too long",
            "items": [1, 2, 3],
            "metadata": {"status": {"result": {"summary": "too deep"}}},
            "price": float("nan"),
        },
        "response": {"description": "x" * 500},
    }

    first = sanitize_observability_payload(raw, policy=policy)
    second = sanitize_observability_payload(raw, policy=policy)
    encoded = json.dumps(first, sort_keys=True, separators=(",", ":"))

    assert first == second
    assert len(encoded.encode()) <= policy.max_bytes
    assert "truncated" in encoded.lower()
    assert "NaN" not in encoded
