from __future__ import annotations

from app.services.taxonomy import CATEGORY_TAXONOMY


def normalize_customer_sex(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in {"male", "man", "m"}:
        return "male"
    if normalized in {"female", "woman", "f"}:
        return "female"
    if normalized in {"nonbinary", "non-binary", "nb", "other"}:
        return "nonbinary"
    return normalized or None


def _normalized_category_genders(category: str) -> set[str]:
    config = CATEGORY_TAXONOMY.get(category)
    if not config:
        return set()
    raw = config.get("genders")
    if not isinstance(raw, list):
        return set()
    return {str(item).strip().lower() for item in raw if item}


def category_allowed_for_sex(category: str, customer_sex: str | None) -> bool:
    normalized_sex = normalize_customer_sex(customer_sex)
    if normalized_sex is None:
        return True

    genders = _normalized_category_genders(category)
    if not genders:
        return True
    if "unisex" in genders:
        return True

    if normalized_sex == "male":
        return bool(genders & {"men", "male", "boys"})
    if normalized_sex == "female":
        return bool(genders & {"women", "female", "girls"})
    if normalized_sex == "nonbinary":
        return bool(genders & {"unisex"})
    return True


def top_style_categories(style_vector: dict, customer_sex: str | None, limit: int = 3) -> list[str]:
    ranked: list[tuple[str, float]] = []
    for key, value in style_vector.items():
        try:
            score = float(value)
        except Exception:
            continue
        ranked.append((str(key), score))

    ranked.sort(key=lambda item: item[1], reverse=True)
    allowed = [category for category, _ in ranked if category_allowed_for_sex(category, customer_sex)]
    return allowed[:limit]


def _normalize_product_gender(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in {"men", "male", "man", "boys", "boy"}:
        return "male"
    if normalized in {"women", "female", "woman", "girls", "girl"}:
        return "female"
    if normalized in {"unisex"}:
        return "unisex"
    return None


def product_allowed_for_sex(product_gender: str | None, category: str | None, customer_sex: str | None) -> bool:
    normalized_sex = normalize_customer_sex(customer_sex)
    if normalized_sex is None:
        return True

    normalized_product_gender = _normalize_product_gender(product_gender)
    if normalized_product_gender == "unisex":
        return True
    if normalized_product_gender == "male":
        return normalized_sex == "male"
    if normalized_product_gender == "female":
        return normalized_sex == "female"

    if category:
        return category_allowed_for_sex(category, normalized_sex)
    return True
