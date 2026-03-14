from __future__ import annotations

import hashlib

from app.config import get_settings

_CATEGORY_FAMILIES = {
    "womens_apparel": ("dresses-aurora", "dresses-nocturne"),
    "shoes": ("shoes-chrome", "shoes-velvet"),
    "handbags": ("handbags-atelier", "handbags-marble"),
    "beauty": ("beauty-amber", "beauty-bloom"),
    "mens_apparel": ("mens-tailor", "mens-midnight"),
    "jewelry_accessories": ("accessories-gilded", "accessories-orbit"),
    "home": ("home-salon", "home-studio"),
    "kids": ("kids-confetti", "kids-spark"),
}


def _asset_base_url() -> str:
    return get_settings().public_base_url.rstrip("/") + "/ui-assets"


def demo_image_url(category: str | None, stable_key: str, variant_hint: str | None = None) -> str:
    family = _CATEGORY_FAMILIES.get(category or "", ("editorial-fallback",))
    key = f"{stable_key}:{variant_hint or category or 'fallback'}".encode()
    digest = hashlib.sha256(key).hexdigest()
    asset_name = family[int(digest[:8], 16) % len(family)]
    return f"{_asset_base_url()}/demo/{asset_name}.svg"
