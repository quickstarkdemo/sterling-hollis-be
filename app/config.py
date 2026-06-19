from functools import lru_cache
from pathlib import Path
from typing import Literal
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
    cors_allowed_origins: str = (
        "http://localhost:8000,"
        "http://127.0.0.1:8000,"
        "http://localhost:5173,"
        "http://127.0.0.1:5173,"
        "http://localhost:3000,"
        "http://127.0.0.1:3000,"
        "https://sterling-hollis-fe.quickstark.com,"
        "https://sterling-hollis.quickstark.com"
    )

    openai_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536
    image_analysis_model: str = "gpt-5.5"
    image_analysis_detail: str = "auto"
    image_upload_max_bytes: int = 8 * 1024 * 1024
    chat_orchestration_model: str = "gpt-5.4-mini"
    chat_orchestration_min_confidence: float = 0.55
    chat_orchestration_mode: Literal["deterministic", "strands_product"] = "deterministic"
    demo_observability_enabled: bool = False
    demo_observability_mode: Literal["off", "latency", "error", "latency_and_error", "network_outage"] = "off"
    demo_observability_latency_seconds: float = 8.0
    demo_observability_target_store_id: str | None = "1001"
    demo_observability_network_event_count: int = 3
    demo_observability_clerk_authorized_emails: str = ""
    demo_observability_clerk_authorized_subjects: str = ""
    catalog_studio_clerk_authorized_emails: str = ""
    catalog_studio_clerk_authorized_subjects: str = ""
    catalog_studio_admin_claim_path: str = ""
    catalog_studio_admin_claim_value: str = "admin"
    catalog_studio_responses_model: str = "gpt-5.5"
    catalog_studio_moderation_model: str = "omni-moderation-latest"
    catalog_studio_responses_timeout_seconds: float = 60.0
    catalog_studio_responses_max_output_tokens: int = 2500
    catalog_studio_realtime_enabled: bool = False
    catalog_studio_realtime_model: str = "gpt-realtime-2"
    catalog_studio_realtime_transcription_model: str = "gpt-4o-mini-transcribe"
    catalog_studio_realtime_client_secret_ttl_seconds: int = 600
    catalog_studio_realtime_timeout_seconds: float = 15.0
    catalog_studio_realtime_safety_identifier_secret: str = ""
    catalog_studio_shared_workflows: bool = False
    catalog_studio_trace_retention_days: int = 7
    catalog_studio_trace_max_depth: int = 6
    catalog_studio_trace_max_string_length: int = 1000
    catalog_studio_trace_max_array_length: int = 25
    catalog_studio_trace_max_object_keys: int = 50
    catalog_studio_trace_max_bytes: int = 16384
    catalog_studio_trace_redacted_keys: str = ""
    api_trace_capture_enabled: bool = False
    api_trace_payload_retention_hours: int = 24
    api_trace_metadata_retention_days: int = 7
    api_trace_max_depth: int = 6
    api_trace_max_string_length: int = 1000
    api_trace_max_array_length: int = 25
    api_trace_max_object_keys: int = 50
    api_trace_max_bytes: int = 16384
    api_trace_redacted_keys: str = ""
    api_trace_max_spans: int = 100
    api_trace_max_links: int = 100
    api_trace_max_events: int = 250
    api_trace_max_artifacts: int = 50
    api_trace_stream_poll_seconds: float = 0.5
    api_trace_stream_keepalive_seconds: float = 15.0
    catalog_studio_media_allowed_hosts: str = ""
    catalog_studio_media_fetch_max_bytes: int = 8 * 1024 * 1024
    catalog_studio_media_fetch_timeout_seconds: float = 15.0
    catalog_studio_media_fetch_max_redirects: int = 3
    catalog_source_output_dir: str = "data/catalog-sources"
    catalog_source_upload_max_bytes: int = 8 * 1024 * 1024
    catalog_source_max_assets_per_bundle: int = 20
    catalog_source_max_dimension: int = 12_000
    catalog_source_max_pixels: int = 40_000_000
    catalog_source_thumbnail_size: int = 320
    enable_legacy_admin_routes: bool = True
    dd_site: str = "datadoghq.com"
    dd_api_key: str | None = None

    pinecone_api_key: str | None = None
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    pinecone_index_name: str = "fashion-products-v1"
    pinecone_catalog_namespace: str = "catalog_products_v1"

    clerk_issuer: str | None = None
    clerk_jwks_url: str | None = None
    clerk_authorized_parties: str = (
        "http://localhost,http://127.0.0.1,"
        "https://sterling-hollis-fe.quickstark.com,"
        "https://sterling-hollis.quickstark.com"
    )
    clerk_demo_customer_id: str | None = None
    clerk_demo_customer_email: str | None = None

    twilio_account_sid: str | None = None
    twilio_api_key_sid: str | None = None
    twilio_api_key_secret: str | None = None
    twilio_sender_number: str | None = None
    twilio_test_to_number: str | None = None

    ses_region: str | None = None
    ses_from_email: str | None = None
    amazon_key_id: str | None = None
    amazon_key_secret: str | None = None

    vector_top_k: int = 50
    index_worker_poll_seconds: float = 2.0
    index_worker_max_idle_seconds: float = 30.0
    index_worker_stale_recovery_seconds: float = 60.0
    image_job_admin_stale_recovery_seconds: float = 60.0

    enable_mcp_adapter: bool = True
    enable_openai_apps_ui: bool = True

    product_image_model: str = "gpt-image-2"
    product_image_size: str = "1024x1024"
    product_image_quality: str = "medium"
    product_image_output_format: str = "jpeg"
    product_image_output_dir: str = "data/product-images"
    product_image_url_path: str = "/product-images"
    product_image_detail_count: int = 3
    product_image_thumbnail_size: int = 320
    product_image_request_timeout_seconds: float = 300.0
    product_image_job_stale_seconds: float = 900.0

    exec_auto_optimize_enabled: bool = False
    strategy_packet_enabled: bool = False
    merch_strategy_context_enabled: bool = False
    associate_priority_tags_enabled: bool = False


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
