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
