"""Validated market configuration and bilingual catalog resolution."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

CATALOGS = json.loads(Path(__file__).with_name("catalogs.json").read_text(encoding="utf-8"))
COUNTRY = os.getenv("OPENSOOQ_COUNTRY", "jo").strip().lower()
if COUNTRY not in CATALOGS:
    raise ValueError("OPENSOOQ_COUNTRY must be jo or kw")
LANGUAGE = os.getenv("OPENSOOQ_LANGUAGE", "en").strip().lower()
if LANGUAGE not in {"en", "ar"}:
    raise ValueError("OPENSOOQ_LANGUAGE must be en or ar")
PROFILE = CATALOGS[COUNTRY]
BASE_URL = f"https://{COUNTRY}.opensooq.com"
CURRENCY = PROFILE["currency"]
CATEGORIES = {int(k): v for k, v in PROFILE["categories"].items()}
CITY_DETAILS = {int(k): v for k, v in PROFILE["cities"].items()}
CITIES = {k: v["en"] for k, v in CITY_DETAILS.items()}

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower().translate(_DIGITS)
    value = re.sub(r"[\u064b-\u065f\u0670ـ]", "", value)
    value = value.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي"}))
    return " ".join(value.split())


def resolve(value: str | None, catalog: dict, label: str) -> int | None:
    if value is None:
        return None
    key = normalize_text(value)
    if not key:
        raise ValueError(f"{label} cannot be empty")
    if key.isdigit():
        # Numeric subcategory IDs may be passed even when absent from the top-level catalog.
        return int(key)
    exact, partial = [], []
    for ident, meta in catalog.items():
        names = [normalize_text(meta.get(k, "")) for k in ("en", "ar", "url")]
        if key in names:
            exact.append(ident)
        elif any(key in name for name in names if name):
            partial.append(ident)
    matches = exact or partial
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError(f"Ambiguous {label} '{value}'; use a full name or ID")
    raise ValueError(
        f"Unknown {label} '{value}' for {PROFILE['name']}; list available {label}s first"
    )


def resolve_city(value: str | None) -> int | None:
    ident = resolve(value, CITY_DETAILS, "city")
    if ident is not None and ident not in CITY_DETAILS:
        raise ValueError(f"Unknown city ID {ident} for {PROFILE['name']}")
    return ident


def resolve_category(value: str | None) -> int | None:
    return resolve(value, CATEGORIES, "category")
