from __future__ import annotations

from app.config import get_settings

from ddtrace.llmobs.decorators import retrieval
from app.observability.llmobs import annotate_safe

try:
    from pinecone import Pinecone, ServerlessSpec
except Exception:  # pragma: no cover
    Pinecone = None  # type: ignore
    ServerlessSpec = None  # type: ignore


class PineconeService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.enabled = bool(self.settings.pinecone_api_key and Pinecone is not None)
        self.client = None
        self.index = None
        if self.enabled:
            self.client = Pinecone(api_key=self.settings.pinecone_api_key)
            self._ensure_index()

    def _ensure_index(self) -> None:
        assert self.client is not None
        name = self.settings.pinecone_index_name
        raw_indexes = self.client.list_indexes()
        if hasattr(raw_indexes, "names"):
            indexes = set(raw_indexes.names())
        else:
            indexes = {idx["name"] for idx in raw_indexes}
        if name not in indexes:
            self.client.create_index(
                name=name,
                dimension=self.settings.embedding_dimension,
                metric="cosine",
                spec=ServerlessSpec(
                    cloud=self.settings.pinecone_cloud,
                    region=self.settings.pinecone_region,
                ),
            )
        self.index = self.client.Index(name)

    def upsert(self, namespace: str, vectors: list[dict]) -> None:
        if not self.enabled or not vectors:
            return
        assert self.index is not None
        self.index.upsert(vectors=vectors, namespace=namespace)

    @retrieval(
        name="pinecone_catalog_query", model_name="pinecone", model_provider="pinecone"
    )
    def query(
        self,
        namespace: str,
        vector: list[float],
        top_k: int,
        filters: dict | None = None,
    ) -> list[dict]:

        annotate_safe(
            input_data={
                "namespace": namespace,
                "top_k": top_k,
                "filters": filters or {},
                "vector_dimension": len(vector),
            },
            metadata={
                "index_name": self.settings.pinecone_index_name,
                "enabled": self.enabled,
            },
        )

        if not self.enabled:
            return []
        assert self.index is not None
        resp = self.index.query(
            namespace=namespace,
            vector=vector,
            top_k=top_k,
            include_metadata=True,
            filter=filters,
        )
        resp_matches = (
            resp.matches if hasattr(resp, "matches") else resp.get("matches", [])
        )
        matches = []
        for m in resp_matches:
            matches.append(
                {
                    "id": m.id if hasattr(m, "id") else m.get("id"),
                    "score": (
                        float(m.score) if hasattr(m, "score") else m.get("score", 0.0)
                    ),
                    "metadata": (
                        m.metadata if hasattr(m, "metadata") else m.get("metadata", {})
                    ),
                }
            )
            annotate_safe(
                output_data=[
                    {
                        "id": match["id"],
                        "score": match["score"],
                        "name": match["metadata"].get("title") or match["id"],
                    }
                    for match in matches[:10]
                ],
                metadata={"match_count": len(matches)},
            )

        return matches

    def status(self, probe: bool = False) -> dict:
        status = {
            "configured": bool(self.settings.pinecone_api_key),
            "client_available": Pinecone is not None,
            "enabled": self.enabled,
            "index_name": self.settings.pinecone_index_name,
            "cloud": self.settings.pinecone_cloud,
            "region": self.settings.pinecone_region,
            "dimension": self.settings.embedding_dimension,
            "probe_attempted": probe,
            "probe_ok": None,
            "probe_error": None,
        }
        if not probe or not self.enabled:
            return status

        try:
            assert self.client is not None
            raw_indexes = self.client.list_indexes()
            if hasattr(raw_indexes, "names"):
                indexes = set(raw_indexes.names())
            else:
                indexes = {idx["name"] for idx in raw_indexes}
            status["probe_ok"] = self.settings.pinecone_index_name in indexes
            if not status["probe_ok"]:
                status["probe_error"] = (
                    f"index {self.settings.pinecone_index_name} not found"
                )
        except Exception as exc:  # pragma: no cover - external dependency
            status["probe_ok"] = False
            status["probe_error"] = str(exc)[:500]
        return status
