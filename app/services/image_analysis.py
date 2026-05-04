from __future__ import annotations

import base64
import json
from io import BytesIO
from typing import Any

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError

from app.catalog.schemas import ImageAnalysisAttributes, ImageAnalysisResponse
from app.config import get_settings
from app.schemas import StyleConstraints
from app.services.taxonomy import CATEGORY_TAXONOMY

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MIME_TO_PIL_FORMAT = {
    "image/jpeg": {"JPEG"},
    "image/png": {"PNG"},
    "image/webp": {"WEBP"},
}


class ImageUploadError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


IMAGE_ANALYSIS_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "target_categories",
        "exclude_categories",
        "target_genders",
        "colors",
        "materials",
        "patterns",
        "style_keywords",
        "occasion_keywords",
        "confidence",
    ],
    "properties": {
        "summary": {"type": "string"},
        "target_categories": {"type": "array", "items": {"type": "string"}},
        "exclude_categories": {"type": "array", "items": {"type": "string"}},
        "target_genders": {"type": "array", "items": {"type": "string"}},
        "colors": {"type": "array", "items": {"type": "string"}},
        "materials": {"type": "array", "items": {"type": "string"}},
        "patterns": {"type": "array", "items": {"type": "string"}},
        "style_keywords": {"type": "array", "items": {"type": "string"}},
        "occasion_keywords": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def _clean_string_list(values: list[str], *, limit: int = 8) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        cleaned.append(normalized)
        seen.add(normalized)
        if len(cleaned) >= limit:
            break
    return cleaned


def validate_image_bytes(
    image_bytes: bytes, content_type: str | None, *, max_bytes: int
) -> str:
    mime_type = str(content_type or "").split(";")[0].strip().lower()
    if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise ImageUploadError(
            "Unsupported image type. Use JPEG, PNG, or WebP.", status_code=415
        )
    if not image_bytes:
        raise ImageUploadError("Image upload is empty.")
    if len(image_bytes) > max_bytes:
        raise ImageUploadError(
            f"Image exceeds the {max_bytes} byte upload limit.", status_code=413
        )

    try:
        with Image.open(BytesIO(image_bytes)) as image:
            detected_format = str(image.format or "").upper()
            image.verify()
    except (SyntaxError, UnidentifiedImageError, OSError) as exc:
        raise ImageUploadError("Uploaded file is not a valid image.") from exc

    expected_formats = _MIME_TO_PIL_FORMAT.get(mime_type, set())
    if expected_formats and detected_format not in expected_formats:
        raise ImageUploadError(
            "Uploaded image content does not match its content type."
        )
    return mime_type


async def read_validated_image(
    upload: UploadFile, *, max_bytes: int | None = None
) -> tuple[bytes, str]:
    settings = get_settings()
    limit = max_bytes or settings.image_upload_max_bytes
    image_bytes = await upload.read(limit + 1)
    mime_type = validate_image_bytes(image_bytes, upload.content_type, max_bytes=limit)
    return image_bytes, mime_type


def _extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output_items = getattr(response, "output", None)
    if output_items is None and isinstance(response, dict):
        output_items = response.get("output")
    if not output_items:
        return ""

    chunks: list[str] = []
    for output in output_items:
        content_items = getattr(output, "content", None)
        if content_items is None and isinstance(output, dict):
            content_items = output.get("content")
        if not content_items:
            continue
        for content in content_items:
            text = getattr(content, "text", None)
            if text is None and isinstance(content, dict):
                text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


def _normalize_analysis(payload: dict) -> ImageAnalysisAttributes:
    raw_categories = _clean_string_list(
        list(payload.get("target_categories") or []), limit=5
    )
    target_categories = [
        category for category in raw_categories if category in CATEGORY_TAXONOMY
    ]
    raw_excluded = _clean_string_list(
        list(payload.get("exclude_categories") or []), limit=5
    )
    exclude_categories = [
        category for category in raw_excluded if category in CATEGORY_TAXONOMY
    ]
    style_keywords = _clean_string_list(
        list(payload.get("style_keywords") or []), limit=8
    )
    style_keywords.extend(
        _clean_string_list(list(payload.get("colors") or []), limit=4)
    )
    style_keywords.extend(
        _clean_string_list(list(payload.get("materials") or []), limit=4)
    )
    style_keywords.extend(
        _clean_string_list(list(payload.get("patterns") or []), limit=4)
    )

    return ImageAnalysisAttributes(
        summary=str(payload.get("summary") or "").strip(),
        target_categories=target_categories,
        exclude_categories=exclude_categories,
        target_genders=list(payload.get("target_genders") or []),
        colors=_clean_string_list(list(payload.get("colors") or []), limit=8),
        materials=_clean_string_list(list(payload.get("materials") or []), limit=8),
        patterns=_clean_string_list(list(payload.get("patterns") or []), limit=8),
        style_keywords=_clean_string_list(style_keywords, limit=12),
        occasion_keywords=_clean_string_list(
            list(payload.get("occasion_keywords") or []), limit=8
        ),
        confidence=float(payload.get("confidence") or 0.0),
    )


def style_constraints_from_analysis(
    analysis: ImageAnalysisAttributes,
) -> StyleConstraints:
    return StyleConstraints(
        constraint_source="consumer_image",
        target_categories=analysis.target_categories,
        exclude_categories=analysis.exclude_categories,
        target_genders=analysis.target_genders,
        style_keywords=analysis.style_keywords,
    )


def image_analysis_query_text(analysis: ImageAnalysisAttributes) -> str:
    parts = [
        analysis.summary,
        "Categories: " + ", ".join(analysis.target_categories),
        "Colors: " + ", ".join(analysis.colors),
        "Materials: " + ", ".join(analysis.materials),
        "Patterns: " + ", ".join(analysis.patterns),
        "Style: " + ", ".join(analysis.style_keywords),
        "Occasions: " + ", ".join(analysis.occasion_keywords),
    ]
    return "\n".join(part for part in parts if part and not part.endswith(": "))


class ImageAnalysisService:
    def __init__(self, client: Any | None = None) -> None:
        self.settings = get_settings()
        self.model = self.settings.image_analysis_model
        self.detail = self.settings.image_analysis_detail
        if client is not None:
            self.client = client
        elif self.settings.openai_api_key and OpenAI is not None:
            self.client = OpenAI(api_key=self.settings.openai_api_key)
        else:
            self.client = None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def analyze(
        self, image_bytes: bytes, mime_type: str, *, context: str | None = None
    ) -> ImageAnalysisResponse:
        if self.client is None:
            raise RuntimeError("OpenAI image analysis is not configured.")

        categories = ", ".join(sorted(CATEGORY_TAXONOMY))
        context_text = (
            f"\nFrontend context: {context.strip()}"
            if context and context.strip()
            else ""
        )
        prompt = (
            "Analyze the uploaded consumer fashion or retail inspiration image for product recommendation. "
            "Extract only visible, product-search useful attributes. Use category IDs from this list when applicable: "
            f"{categories}. Keep keywords concise and avoid personal or biometric identification.{context_text}"
        )
        image_url = (
            f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        )

        response = self.client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": image_url,
                            "detail": self.detail,
                        },
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "consumer_image_analysis",
                    "strict": True,
                    "schema": IMAGE_ANALYSIS_JSON_SCHEMA,
                }
            },
        )
        raw_text = _extract_response_text(response)
        if not raw_text:
            raise RuntimeError("OpenAI image analysis returned no text output.")
        analysis = _normalize_analysis(json.loads(raw_text))

        return ImageAnalysisResponse(
            analysis=analysis,
            style_constraints=style_constraints_from_analysis(analysis),
            model=self.model,
            image_discarded=True,
        )
