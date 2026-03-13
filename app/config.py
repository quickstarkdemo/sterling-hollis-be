from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Fashion Product DB"
    environment: str = "dev"

    database_url: str | None = None
    pghost: str | None = None
    pgport: int = 5432
    pgdatabase: str | None = None
    pguser: str | None = None
    pgpassword: str | None = None

    data_dir: str = "data/runs"

    openai_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536

    pinecone_api_key: str | None = None
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    pinecone_index_name: str = "fashion-products-v1"

    vector_top_k: int = 50


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.database_url:
        if settings.pghost and settings.pgdatabase and settings.pguser and settings.pgpassword is not None:
            user = quote_plus(settings.pguser)
            password = quote_plus(settings.pgpassword)
            settings.database_url = (
                f"postgresql+psycopg://{user}:{password}@{settings.pghost}:{settings.pgport}/{settings.pgdatabase}"
            )
        else:
            settings.database_url = "postgresql+psycopg://postgres:postgres@localhost:5432/productdb"
    return settings
