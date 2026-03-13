from __future__ import annotations

from app.config import get_settings
from app.services.embeddings import EmbeddingService, OpenAI
from app.services.pinecone_service import Pinecone, PineconeService


def vector_mode(openai_enabled: bool, pinecone_enabled: bool) -> str:
    if openai_enabled and pinecone_enabled:
        return "cloud_full"
    if openai_enabled:
        return "openai_only"
    if pinecone_enabled:
        return "pinecone_with_fallback_embeddings"
    return "local_fallback"


def vector_status_payload(probe: bool = False) -> dict:
    settings = get_settings()

    try:
        embedding_service = EmbeddingService()
        openai_status = embedding_service.status(probe=probe)
    except Exception as exc:
        openai_status = {
            "configured": bool(settings.openai_api_key),
            "client_available": OpenAI is not None,
            "enabled": False,
            "model": settings.embedding_model,
            "dimension": settings.embedding_dimension,
            "probe_attempted": probe,
            "probe_ok": False if probe else None,
            "probe_error": str(exc)[:500],
        }

    try:
        pinecone_service = PineconeService()
        pinecone_status = pinecone_service.status(probe=probe)
    except Exception as exc:
        pinecone_status = {
            "configured": bool(settings.pinecone_api_key),
            "client_available": Pinecone is not None,
            "enabled": False,
            "index_name": settings.pinecone_index_name,
            "cloud": settings.pinecone_cloud,
            "region": settings.pinecone_region,
            "dimension": settings.embedding_dimension,
            "probe_attempted": probe,
            "probe_ok": False if probe else None,
            "probe_error": str(exc)[:500],
        }

    return {
        "mode": vector_mode(bool(openai_status["enabled"]), bool(pinecone_status["enabled"])),
        "openai": openai_status,
        "pinecone": pinecone_status,
    }
