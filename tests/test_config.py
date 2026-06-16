from __future__ import annotations

from app.config import get_settings
from app.services import demo_observability


def test_database_url_builds_from_pg_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("PGHOST", "192.168.1.200")
    monkeypatch.setenv("PGPORT", "9001")
    monkeypatch.setenv("PGDATABASE", "products")
    monkeypatch.setenv("PGUSER", "postgres")
    monkeypatch.setenv("PGPASSWORD", "Vall123@")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.database_url == "postgresql+psycopg://postgres:Vall123%40@192.168.1.200:9001/products"
    get_settings.cache_clear()


def test_strategy_flags_default_to_false(monkeypatch):
    for env_key in [
        "EXEC_AUTO_OPTIMIZE_ENABLED",
        "STRATEGY_PACKET_ENABLED",
        "MERCH_STRATEGY_CONTEXT_ENABLED",
        "ASSOCIATE_PRIORITY_TAGS_ENABLED",
    ]:
        monkeypatch.setenv(env_key, "false")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.exec_auto_optimize_enabled is False
    assert settings.strategy_packet_enabled is False
    assert settings.merch_strategy_context_enabled is False
    assert settings.associate_priority_tags_enabled is False
    get_settings.cache_clear()


def test_demo_observability_defaults_from_env(monkeypatch):
    monkeypatch.setenv("DEMO_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("DEMO_OBSERVABILITY_MODE", "latency")
    monkeypatch.setenv("DEMO_OBSERVABILITY_LATENCY_SECONDS", "3.5")
    monkeypatch.setenv("DEMO_OBSERVABILITY_TARGET_STORE_ID", "2002")
    monkeypatch.setattr(demo_observability, "_STATE", None)
    get_settings.cache_clear()

    settings = get_settings()
    state = demo_observability.get_demo_observability_state()

    assert settings.demo_observability_enabled is True
    assert settings.demo_observability_mode == "latency"
    assert settings.demo_observability_latency_seconds == 3.5
    assert settings.demo_observability_target_store_id == "2002"
    assert state.enabled is True
    assert state.mode == "latency"
    assert state.latency_seconds == 3.5
    assert state.target_store_id == "2002"
    get_settings.cache_clear()


def test_catalog_studio_trace_limits_load_from_env(monkeypatch):
    monkeypatch.setenv("CATALOG_STUDIO_RESPONSES_MODEL", "gpt-5.5-test")
    monkeypatch.setenv("CATALOG_STUDIO_MODERATION_MODEL", "omni-moderation-test")
    monkeypatch.setenv("CATALOG_STUDIO_RESPONSES_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("CATALOG_STUDIO_RESPONSES_MAX_OUTPUT_TOKENS", "1800")
    monkeypatch.setenv("CATALOG_STUDIO_SHARED_DEMO_RUNS", "true")
    monkeypatch.setenv("CATALOG_STUDIO_TRACE_RETENTION_DAYS", "3")
    monkeypatch.setenv("CATALOG_STUDIO_TRACE_MAX_BYTES", "8192")
    monkeypatch.setenv("CATALOG_STUDIO_TRACE_REDACTED_KEYS", "internal_note,vendor_secret")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.catalog_studio_responses_model == "gpt-5.5-test"
    assert settings.catalog_studio_moderation_model == "omni-moderation-test"
    assert settings.catalog_studio_responses_timeout_seconds == 45
    assert settings.catalog_studio_responses_max_output_tokens == 1800
    assert settings.catalog_studio_shared_demo_runs is True
    assert settings.catalog_studio_trace_retention_days == 3
    assert settings.catalog_studio_trace_max_bytes == 8192
    assert settings.catalog_studio_trace_redacted_keys == "internal_note,vendor_secret"
    get_settings.cache_clear()
