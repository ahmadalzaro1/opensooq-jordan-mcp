"""Evidence-preserving vehicle/property fields; missing or conflicting facts stay unknown."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from market import normalize_text


def _key(value):
    return re.sub(r"[^\w]", "", normalize_text(str(value))).replace("_", "")


VEHICLE = {
    "make": ("car_make", "make", "manufacturer", "نوع السيارة", "الشركة المصنعة"),
    "model": ("car_model", "model", "الموديل", "موديل"),
    "trim": ("car_trim", "trim", "الفئة"),
    "year": ("Car_Year", "year", "model year", "سنة الصنع", "سنه الصنع"),
    "mileage_km": ("Kilometers_Cars", "kilometers", "mileage", "المسافة المقطوعة", "الكيلومترات"),
    "fuel": ("fuel", "fuel type", "car_fuel", "نوع الوقود"),
    "transmission": ("transmission", "car_transmission", "ناقل الحركة", "ناقل الحركه"),
    "engine_size": ("engine size", "engine", "car_engine_size", "سعة المحرك", "سعه المحرك"),
    "body_type": ("Cars_body_types", "body type", "نوع الهيكل"),
    "vehicle_condition": ("ConditionUsed", "condition", "الحالة", "حالة السيارة"),
}
PROPERTY = {
    "bedrooms": ("Rooms", "bedrooms", "number of bedrooms", "عدد الغرف", "غرف النوم"),
    "bathrooms": ("Bathrooms", "number of bathrooms", "عدد الحمامات", "حمامات"),
    "area_m2": ("Surface", "area", "surface area", "المساحة", "المساحه"),
    "furnished": ("Furnished_RealEstate", "furnished", "furnishing", "مفروش", "التاثيث"),
    "floor": ("Floors", "floor", "الطابق"),
    "rental_period": ("RentalPeriod", "rental period", "مدة الايجار", "فترة الايجار"),
    "price_period": (
        "rent billing period",
        "price period",
        "payment period",
        "دورية الدفع",
        "فترة دفع الايجار",
    ),
}
ALIASES = {
    "تويوتا": "toyota",
    "بريوس": "prius",
    "هيونداي": "hyundai",
    "هونداي": "hyundai",
    "ايونيك": "ioniq",
    "ايونيق": "ioniq",
    "كيا": "kia",
    "نيسان": "nissan",
    "اوتوماتيك": "automatic",
    "automatic": "automatic",
    "اتوماتيك": "automatic",
    "بنزين": "gasoline",
    "petrol": "gasoline",
    "هايبرد": "hybrid",
    "هجين": "hybrid",
    "كهرباء": "electric",
    "كهربائي": "electric",
    "ديزل": "diesel",
    "مستعمل": "used",
    "جديد": "new",
}


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


def _parse(field, row):
    text = normalize_text(str(row.get("value", "")))
    if not text:
        return None
    if field in {"year", "bedrooms", "bathrooms", "floor"}:
        if field == "bedrooms" and text in {"studio", "ستوديو", "استوديو"}:
            return 0
        if field == "floor" and text in {"ground floor", "ground", "ارضي", "الارضي"}:
            return 0
        found = re.fullmatch(r"(\d+)(?:\s+(?:bedrooms?|bathrooms?|غرف|حمامات))?", text)
        if not found:
            return None
        number = int(found[1])
        if field == "year":
            return number if 1900 <= number <= datetime.now(timezone.utc).year + 1 else None
        return number if number <= 100 else None
    if field == "mileage_km":
        # Explicit units or the verified Kilometers_Cars attribute establish kilometres.
        unit_text = normalize_text(str(row.get("label", "")) + " " + text)
        miles = bool(re.search(r"\bmiles?\b|ميل", unit_text))
        km = bool(re.search(r"kilometer|\bkm\b|كيلومتر|كلم", unit_text))
        if not (miles or km):
            return None
        cleaned = re.sub(r"[,٬]", "", text)
        found = re.fullmatch(
            r"(\d+(?:\.\d+)?)(?:\s*[-–]\s*(\d+(?:\.\d+)?))?\s*(?:km|kilometers?|miles?|كيلومتر|كلم|ميل)?",
            cleaned,
        )
        if not found:
            return None
        lo = float(found[1])
        hi = float(found[2] or found[1])
        if hi < lo or hi > 10_000_000:
            return None
        factor = 1.609344 if miles else 1
        return {"min": round(lo * factor, 3), "max": round(hi * factor, 3), "exact": lo == hi}
    if field == "area_m2":
        # A bare Surface number is not enough to establish square-metre units.
        found = re.fullmatch(
            r"([\d,٬]+(?:[.٫]\d+)?)\s*(?:m2|m²|sqm|sq\.?\s*m|م2|متر مربع|م²)", text
        )
        if not found:
            return None
        value = _number(re.sub(r"[,٬]", "", found[1]).replace("٫", "."))
        return value if value and value <= 10_000_000 else None
    if field in {"rental_period", "price_period"}:
        return {
            "day": "daily",
            "daily": "daily",
            "يومي": "daily",
            "يوم": "daily",
            "week": "weekly",
            "weekly": "weekly",
            "اسبوعي": "weekly",
            "month": "monthly",
            "monthly": "monthly",
            "شهري": "monthly",
            "شهر": "monthly",
            "year": "yearly",
            "yearly": "yearly",
            "annual": "yearly",
            "سنوي": "yearly",
            "سنة": "yearly",
        }.get(text)
    if field == "furnished":
        return {
            "not furnished": "unfurnished",
            "unfurnished": "unfurnished",
            "غير مفروش": "unfurnished",
            "furnished": "furnished",
            "مفروش": "furnished",
            "مفروشة": "furnished",
            "partly furnished": "partial",
            "semi furnished": "partial",
            "شبه مفروش": "partial",
        }.get(text)
    return ALIASES.get(text, text)


def specifications(listing):
    category = normalize_text(
        " ".join(
            [
                str(listing.get("category", "")),
                str(listing.get("categoryId", "")),
                *map(str, listing.get("categoryPath") or []),
            ]
        )
    )
    if any(x in category for x in ("carsforsale", "cars for sale", "سيارات للبيع")):
        domain, mapping = "vehicle", VEHICLE
    elif any(x in category for x in ("property", "realestate", "apartment", "عقارات", "شقق")):
        domain, mapping = "property", PROPERTY
    else:
        return {"domain": "general", "fields": {}, "evidence": {}, "conflicts": [], "warnings": []}
    fields = {name: None for name in mapping}
    evidence, conflicts = {}, []
    for field, labels in mapping.items():
        values = []
        for row in listing.get("attributes") or []:
            if not isinstance(row, dict) or _key(row.get("label", "")) not in {
                _key(label) for label in labels
            }:
                continue
            value = _parse(field, row)
            if value is not None:
                evidence.setdefault(field, []).append(
                    {
                        "label": row["label"],
                        "value": row.get("value"),
                        "source": row.get("source", "attributes"),
                    }
                )
                if value not in values:
                    values.append(value)
        if len(values) == 1:
            fields[field] = values[0]
        elif len(values) > 1:
            conflicts.append(field)
    title = normalize_text(str(listing.get("title") or ""))
    if domain == "vehicle" and fields["year"]:
        years = set(map(int, re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", title)))
        if years and years != {fields["year"]}:
            conflicts.append("year_title_mismatch")
            fields["year"] = None
    if domain == "property":
        fields["transaction"] = (
            "rent"
            if "rent" in category or "للايجار" in category
            else "sale"
            if "sale" in category or "للبيع" in category
            else None
        )
        fields["property_type"] = (
            "apartment"
            if "apartment" in category or "شقق" in category
            else "land"
            if "land" in category or "اراضي" in category
            else "villa"
            if "villa" in category or "فلل" in category
            else None
        )
        for name in ("city", "neighborhood"):
            fields[name] = (
                normalize_text(str((listing.get("location") or {}).get(name) or "")) or None
            )
        if "شبه مفروش" in title and fields["furnished"] not in {None, "partial"}:
            conflicts.append("furnished_title_mismatch")
            fields["furnished"] = None
    warnings = ["conflicting_specifications"] if conflicts else []
    if domain == "property" and fields["transaction"] == "rent" and not fields["rental_period"]:
        warnings.append("unknown_rental_period")
    if domain == "property" and fields["transaction"] == "rent" and not fields["price_period"]:
        warnings.append("unconfirmed_rent_price_period")
    return {
        "domain": domain,
        "fields": fields,
        "evidence": evidence,
        "conflicts": conflicts,
        "warnings": warnings,
    }


def domain_cohort(listing):
    spec = listing.get("specifications") or specifications(listing)
    fields = spec["fields"]
    if spec["domain"] == "general":
        return None
    if spec["conflicts"]:
        return f"{spec['domain']}|unknown_conflicting_specifications"
    if spec["domain"] == "vehicle":
        names = (
            "make",
            "model",
            "trim",
            "year",
            "fuel",
            "transmission",
            "body_type",
            "engine_size",
            "vehicle_condition",
        )
        mileage = fields["mileage_km"]
        # Only a range entirely within one 50k band is comparable in that band.
        band = (
            int(mileage["min"] // 50000)
            if mileage and mileage["min"] // 50000 == mileage["max"] // 50000
            else None
        )
        return (
            "vehicle|"
            + "|".join(
                f"{name}={fields[name] if fields[name] is not None else 'unknown'}"
                for name in names
            )
            + f"|mileage_band={band if band is not None else 'unknown'}"
        )
    names = (
        "transaction",
        "property_type",
        "city",
        "neighborhood",
        "bedrooms",
        "bathrooms",
        "furnished",
        "floor",
    )
    area = fields["area_m2"]
    key = "property|" + "|".join(
        f"{name}={fields[name] if fields[name] is not None else 'unknown'}" for name in names
    )
    key += f"|area_band={int(area // 25) if area else 'unknown'}"
    if fields["transaction"] == "rent":
        key += f"|rental_period={fields['rental_period'] or 'unknown'}"
        key += f"|price_period={fields['price_period'] or 'unknown'}"
    return key
