from __future__ import annotations

from app.services.auth.clerk import _authorized_party_allowed


def test_authorized_party_allows_matching_origin_with_path():
    allowed = {"http://localhost:5173", "https://sterling-hollis.quickstark.com"}

    assert _authorized_party_allowed("http://localhost:5173/sign-in/sso-callback", allowed)


def test_authorized_party_allows_any_port_when_allowed_has_no_port():
    allowed = {"http://localhost", "https://clerk.shared.lcl.dev"}

    assert _authorized_party_allowed("http://localhost:5173", allowed)
    assert _authorized_party_allowed("https://clerk.shared.lcl.dev/v1/oauth_callback", allowed)


def test_authorized_party_rejects_unlisted_origin():
    allowed = {"http://localhost:5173"}

    assert not _authorized_party_allowed("https://evil.example", allowed)
