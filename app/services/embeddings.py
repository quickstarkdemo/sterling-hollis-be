from __future__ import annotations

import hashlib
import math
from typing import Sequence

from app.config import get_settings

from app.observability.genai_otel import (
    current_genai_span,
    record_genai_input,
    record_genai_output,
    set_span_attributes,
    trace_genai_embedding,
)

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

    @trace_genai_embedding(
        "openai_text_embeddings",
        model=lambda self, *args, **kwargs: self.model,
        provider="openai",
        attributes=lambda self, texts: {
            "app.embedding.input_count": len(texts),
            "app.embedding.dimension": self.dimension,
            "app.embedding.uses_openai": self.client is not None,
        },
    )
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        span = current_genai_span()

        record_genai_input(
            span,
            [
                {
                    "role": "user",
                    "parts": [
                        {
                            "type": "text",
                            "content": text[:500],
                        }
                    ],
                }
                for text in texts
            ],
        )

        if not texts:
            vectors = []
        elif self.client is None:
            vectors = [self._deterministic_vector(t) for t in texts]
        else:
            resp = self.client.embeddings.create(model=self.model, input=list(texts))
            vectors = [item.embedding for item in resp.data]

        set_span_attributes(
            span,
            {
                "app.embedding.output_count": len(vectors),
                "app.embedding.output_dimension": len(vectors[0]) if vectors else 0,
            },
        )

        record_genai_output(
            span,
            [
                {
                    "role": "assistant",
                    "parts": [
                        {
                            "type": "text",
                            "content": {
                                "vector_count": len(vectors),
                                "dimension": len(vectors[0]) if vectors else 0,
                            },
                        }
                    ],
                }
            ],
        )

        return vectors


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
