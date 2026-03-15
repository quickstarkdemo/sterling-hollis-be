from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Fashion Product DB"
    environment: str = "dev"
    app_build_version: str | None = None

    database_url: str | None = None
    pghost: str | None = None
    pgport: int = 5432
    pgdatabase: str | None = None
    pguser: str | None = None
    pgpassword: str | None = None

    data_dir: str = "data/runs"
    store_source_index_url: str | None = None
    store_source_detail_url_template: str | None = None
    store_source_cache_path: str = "data/store_source_snapshot.json"
    mcp_allowed_hosts: str = "127.0.0.1:*,localhost:*,[::1]:*"
    mcp_allowed_origins: str = "http://127.0.0.1:*,http://localhost:*,http://[::1]:*"
    public_base_url: str = "http://localhost:8000"

    openai_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536

    pinecone_api_key: str | None = None
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    pinecone_index_name: str = "fashion-products-v1"

    twilio_account_sid: str | None = None
    twilio_api_key_sid: str | None = None
    twilio_api_key_secret: str | None = None
    twilio_sender_number: str | None = None
    twilio_test_to_number: str | None = None

    vector_top_k: int = 50
    index_worker_poll_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.app_build_version:
        version_file = Path(__file__).resolve().parent.parent / "VERSION"
        try:
            settings.app_build_version = version_file.read_text(encoding="utf-8").strip() or "dev"
        except OSError:
            settings.app_build_version = "dev"
    if settings.pghost and settings.pgdatabase and settings.pguser and settings.pgpassword is not None:
        user = quote_plus(settings.pguser)
        password = quote_plus(settings.pgpassword)
        settings.database_url = (
            f"postgresql+psycopg://{user}:{password}@{settings.pghost}:{settings.pgport}/{settings.pgdatabase}"
        )
    elif not settings.database_url:
        settings.database_url = "postgresql+psycopg://postgres:postgres@localhost:5432/productdb"
    return settings
