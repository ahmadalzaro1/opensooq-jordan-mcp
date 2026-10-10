"""Conservative bilingual matching and explainable comparison cohorts.

This is deterministic text analysis, not a classifier or a guarantee. Missing
specifications stay unknown; risky offers remain visible in general discovery.
"""

from __future__ import annotations

import re
from collections import Counter

from market import normalize_text

ALIASES = {
    "iphone": ("ايفون", "اي فون", "ايفوون", "i phone"),
    "samsung": ("سامسونج", "سامسونغ"),
    "galaxy": ("جالاكسي", "جالكسي", "غالاكسي", "جلاكسي"),
    "apple": ("ابل", "ابيل"),
    "macbook": ("ماك بوك", "ماكبوك", "mac book"),
    "playstation": ("بلايستيشن", "بلاي ستيشن", "بليستيشن", "play station", "ps"),
    "toyota": ("تويوتا",),
    "prius": ("بريوس", "بريوسس"),
    "hyundai": ("هيونداي", "هونداي"),
    "ioniq": ("ايونيك", "ايونك", "ايونيق"),
    "nintendo": ("نينتندو",),
    "switch": ("سويتش",),
    "pro": ("برو",),
    "max": ("ماكس",),
    "ultra": ("الترا",),
    "s": ("اس",),
    "gb": ("جيجا", "غيغا", "جيجابايت", "جيجابيت"),
    "plus": ("بلس", "بلاس"),
    "mini": ("ميني",),
    "lite": ("لايت",),
    "slim": ("سليم",),
    "oled": ("اوليد", "او ليد"),
    "apartment": ("شقة", "شقه", "شقق"),
    "rent": ("للايجار", "ايجار", "اجار"),
    "sale": ("للبيع",),
    "new": ("جديد", "جديدة"),
    "used": ("مستعمل", "مستعملة"),
}
STOPWORDS = {
    "for",
    "the",
    "and",
    "with",
    "in",
    "of",
    "a",
    "an",
    "gb",
    "في",
    "من",
    "مع",
    "و",
    "sale",
}
TIERS = {"pro", "max", "ultra", "plus", "mini", "lite", "fe"}
IMPLIED_BRANDS = {
    "iphone": "apple",
    "macbook": "apple",
    "galaxy": "samsung",
    "prius": "toyota",
    "ioniq": "hyundai",
}


def canonical_text(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(
        r"(?<!\d)(1|2)\s*(?:tb\b|تيرا)", lambda m: str(int(m.group(1)) * 1024) + " gb", text
    )
    # Normalize compact model/storage forms: S24, PS5, 256GB, آيفون15.
    text = re.sub(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])", " ", text)
    for canonical, aliases in ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            text = re.sub(rf"(?<!\w){re.escape(alias)}(?!\w)", canonical, text)
    text = re.sub(r"(?<=\d)\s*\+", " plus ", text)
    return " ".join(re.findall(r"\w+", text))


def tokens(text: str) -> list[str]:
    return [t for t in canonical_text(text).split() if t not in STOPWORDS]


def _content(listing: dict) -> str:
    attributes = listing.get("attributes") or []
    extra = " ".join(str(a.get("value", "")) for a in attributes if isinstance(a, dict))
    return f"{listing.get('title') or ''} {listing.get('description') or ''} {extra}"


def quality(listing: dict) -> dict:
    title = normalize_text(str(listing.get("title") or ""))
    content = normalize_text(_content(listing))
    title_tokens = set(tokens(title))

    def condition_from(text):
        used = bool(
            re.search(
                r"\bused\b|مستعمل|بحالة الجديد|كالجديد|like new|pre.?owned|second.?hand", text
            )
        )
        if used:
            return "used"
        if re.search(r"\bnew\b|جديد|مختوم|sealed|unopened", text):
            return "new"
        return None

    # A title is more specific than a dealer's generic description of its inventory.
    condition = condition_from(title)
    explicit = []
    for attr in listing.get("attributes") or []:
        if isinstance(attr, dict) and re.search(
            "condition|الحالة", normalize_text(str(attr.get("label", "")))
        ):
            detected = condition_from(normalize_text(str(attr.get("value", ""))))
            if detected:
                explicit.append(detected)
    if explicit:
        condition = (
            explicit[0]
            if len(set(explicit)) == 1 and (condition is None or condition == explicit[0])
            else "unknown"
        )
    elif condition is None:
        condition = condition_from(content)
    condition = condition or "unknown"
    capacities = set()
    # Explicit capacity units or storage labels; do not mistake model years/battery % for storage.
    for match in re.finditer(
        r"(?<!\d)(32|64|128|256|512|1024|2048)\s*(?:gb|g\b|جيجا|غيغا|جيجابايت|جيجابيت)", content
    ):
        capacities.add(int(match.group(1)))
    for match in re.finditer(r"(?<!\d)(1|2)\s*(?:tb|تيرا)", content):
        capacities.add(int(match.group(1)) * 1024)
    for match in re.finditer(
        r"(?:ذاكرة|ذاكره|سعة|سعه|storage|capacity)\s*[:=]?\s*(32|64|128|256|512|1024|2048)(?!\d)",
        content,
    ):
        capacities.add(int(match.group(1)))
    # Common phone title shorthand: "ايفون 15 برو 256" and "128/256".
    if {"iphone", "galaxy"} & title_tokens:
        capacities.update(
            int(x) for x in title_tokens if x in {"32", "64", "128", "256", "512", "1024", "2048"}
        )
    risks = []
    accessory = re.search(
        r"\b(case|cover|charger|cable|screen protector|replacement screen)\b|كفر|كڤر|جراب|غطاء|شاحن|وصلة|وصله|حماية شاشة|حمايه للشاشه|شاشة بديلة",
        title,
    )
    # A complete phone can mention included accessories; require a product-focused title.
    if accessory and not re.search(r"\b(with|including|includes)\b|معه|مع |يشمل|هدية|هديه", title):
        risks.append("accessory")
    if re.search(
        r"\b(installments?|down payment|deposit|monthly payment)\b|تقسيط|اقساط|قسط|دفعة اول[ىي]|دفعه اول[ىي]",
        title,
    ):
        risks.append("payment_offer")
    if re.search(
        r"\b(swap|exchange|trade only|wanted|looking for)\b|للبدل|للتبديل|مطلوب|ابحث عن", title
    ):
        risks.append("wanted_or_exchange")
    damaged = bool(
        re.search(r"\b(broken|damaged|for parts|not working)\b|مكسور|معطل|لا يعمل|قطع غيار", title)
    )
    if damaged:
        risks.append("damaged")
    if len(capacities) > 1:
        risks.append("multiple_capacities")
    tiers = sorted(TIERS & title_tokens)
    normalized_title = canonical_text(title)
    model = re.search(r"\b(iphone|galaxy|samsung)\s+(?:s\s+)?(\d{1,2})\b", normalized_title)
    model_identity = "unknown"
    if model:
        family, generation = model.groups()
        family = "galaxy" if family == "samsung" else family
        model_identity = f"{family} {generation}"
        models = re.findall(r"\b(?:iphone|galaxy|samsung)\s+(?:s\s+)?(\d{1,2})\b", normalized_title)
        if len(set(models)) > 1 or re.search(
            r"(?:iphone|galaxy|samsung)\s+(?:s\s+)?\d{1,2}\s*/\s*\d{1,2}", title
        ):
            risks.append("multiple_models")
    else:
        known = next(
            (
                t
                for t in ("prius", "ioniq", "macbook", "playstation", "switch")
                if t in title_tokens
            ),
            None,
        )
        if known:
            model_identity = known
            if known in {"prius", "ioniq"}:
                years = sorted(t for t in title_tokens if t.isdigit() and 1990 <= int(t) <= 2035)
                model_identity += " " + (years[0] if len(years) == 1 else "year_unknown")
            if known == "playstation":
                generation = re.search(r"playstation\s+(\d)", normalized_title)
                if generation:
                    model_identity += " " + generation.group(1)
    identity = " ".join(t for t in tokens(title) if t in ALIASES or t.isdigit())
    return {
        "condition": condition,
        "storage_gb": next(iter(capacities)) if len(capacities) == 1 else None,
        "storage_options_gb": sorted(capacities),
        "variant": "+".join(tiers) or "base",
        "risk_flags": risks,
        "identity_hint": identity,
        "model_identity": model_identity,
    }


def match_listing(query: str, listing: dict) -> bool:
    required = set(tokens(query))
    if not required:
        return True
    title_tokens = set(tokens(str(listing.get("title") or "")))
    hay = set(tokens(_content(listing)))
    for family, brand in IMPLIED_BRANDS.items():
        if family in hay:
            hay.add(brand)
    if "samsung" in hay and "s" in hay and any(t.isdigit() for t in hay):
        hay.add("galaxy")
    q = quality(listing)
    if q["condition"] != "unknown":
        hay.add(q["condition"])
    # Product-family anchors and explicit generations must occur in the title;
    # a seller comparing another device in the description is not a product match.
    title_hay = set(title_tokens)
    if "samsung" in title_hay and "s" in title_hay and any(t.isdigit() for t in title_hay):
        title_hay.add("galaxy")
    anchors = required & {"iphone", "galaxy", "macbook", "prius", "ioniq", "playstation", "switch"}
    if not anchors <= title_hay:
        return False
    query_model = re.search(r"\b(iphone|galaxy)\s+(?:s\s+)?(\d{1,2})\b", canonical_text(query))
    if query_model:
        if q["model_identity"] != " ".join(query_model.groups()):
            return False
    # Every significant word and number must match: "15 pro" alone is not an iPhone.
    if not required <= hay:
        return False
    if "new" in required and q["condition"] != "new":
        return False
    if "used" in required and q["condition"] != "used":
        return False
    if {"iphone", "galaxy"} & required and any(t.isdigit() for t in required):
        # A specifically named generation uses an exact tier, avoiding Pro/Max mixing.
        if (required & TIERS) != (title_tokens & TIERS):
            return False
    return True


def relevant_listings(query: str, listings: list[dict]) -> list[dict]:
    return [listing for listing in listings if match_listing(query, listing)]


def cohort_key(listing: dict) -> str:
    q = listing.get("quality") or quality(listing)
    return f"{q['model_identity']}|{q['variant']}|storage={q['storage_gb'] or 'unknown'}|condition={q['condition']}"


def filter_quality(
    listings: list[dict], *, condition="all", storage_gb=None, include_risky=False
) -> tuple[list[dict], dict]:
    kept = []
    rejected = Counter()
    for listing in listings:
        q = listing.get("quality") or quality(listing)
        if not include_risky and q["risk_flags"]:
            rejected.update(q["risk_flags"])
            continue
        if condition != "all" and q["condition"] != condition:
            rejected["condition_mismatch"] += 1
            continue
        if storage_gb is not None and q["storage_gb"] != storage_gb:
            rejected["storage_mismatch_or_unknown"] += 1
            continue
        kept.append(listing)
    return kept, dict(rejected)
