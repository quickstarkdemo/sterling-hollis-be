from __future__ import annotations

import hashlib
import math
from typing import Sequence

from app.config import get_settings

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


class EmbeddingService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.model = self.settings.embedding_model
        self.dimension = self.settings.embedding_dimension
        self.client = None
        if self.settings.openai_api_key and OpenAI is not None:
            self.client = OpenAI(api_key=self.settings.openai_api_key)
        self.enabled = self.client is not None

    def _deterministic_vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values: list[float] = []
        idx = 0
        while len(values) < self.dimension:
            b = digest[idx % len(digest)]
            # map byte to [-1, 1]
            values.append((b / 127.5) - 1.0)
            idx += 1
        # normalize
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        elif self.client is None:
            return [self._deterministic_vector(t) for t in texts]

        resp = self.client.embeddings.create(model=self.model, input=list(texts))
        return [item.embedding for item in resp.data]


    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def status(self, probe: bool = False) -> dict:
        status = {
            "configured": bool(self.settings.openai_api_key),
            "client_available": OpenAI is not None,
            "enabled": self.enabled,
            "model": self.model,
            "dimension": self.dimension,
            "probe_attempted": probe,
            "probe_ok": None,
            "probe_error": None,
        }
        if not probe or not self.enabled:
            return status

        try:
            vector = self.embed_text("vector status probe")
            status["probe_ok"] = len(vector) == self.dimension
            if not status["probe_ok"]:
                status["probe_error"] = (
                    f"expected dimension {self.dimension}, got {len(vector)}"
                )
        except Exception as exc:  # pragma: no cover - external dependency
            status["probe_ok"] = False
            status["probe_error"] = str(exc)[:500]
        return status
