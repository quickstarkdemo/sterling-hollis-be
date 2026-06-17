from __future__ import annotations


AUTHORING_METADATA_KEY = "_catalog_studio_authoring"


def public_product_metadata(metadata: dict | None) -> dict:
    return {
        key: value
        for key, value in dict(metadata or {}).items()
        if key != AUTHORING_METADATA_KEY
    }


def authoring_metadata(metadata: dict | None) -> dict:
    value = dict(metadata or {}).get(AUTHORING_METADATA_KEY)
    return dict(value) if isinstance(value, dict) else {}


def persisted_product_metadata(
    metadata: dict,
    *,
    design_specification: dict | None,
    variant_axes: list[str],
    primary_variant_id: str,
) -> dict:
    return {
        **public_product_metadata(metadata),
        AUTHORING_METADATA_KEY: {
            "design_specification": design_specification,
            "variant_axes": variant_axes,
            "primary_variant_id": primary_variant_id,
        },
    }
