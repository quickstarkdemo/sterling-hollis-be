from __future__ import annotations

from app.config import get_settings


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
