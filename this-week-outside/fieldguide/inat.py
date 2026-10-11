# What has been spotted near you lately — from iNaturalist's open observation data.

from __future__ import annotations
import datetime as dt
from . import http
SPECIES_COUNTS_URL = "https://api.inaturalist.org/v1/observations/species_counts"
OBSERVATIONS_URL = "https://api.inaturalist.org/v1/observations"

# (key, label, emoji) — display order on the card
GROUPS = [
    ("Aves", "Birds", "🐦"),
    ("Plantae", "Plants & flowers", "🌼"),
    ("Fungi", "Fungi", "🍄"),
    ("Insecta", "Insects", "🦋"),
    ("Mammalia", "Mammals", "🦌"),
    ("Herps", "Reptiles & amphibians", "🦎"),
    ("Arachnida", "Spiders & kin", "🕷️"),
    ("Other", "Everything else", "🐾"),
]
_GROUP_META = {key: (label, emoji) for key, label, emoji in GROUPS}
_ICONIC_TO_GROUP = {
    "Aves": "Aves",
    "Plantae": "Plantae",
    "Fungi": "Fungi",
    "Insecta": "Insecta",
    "Mammalia": "Mammalia",
    "Reptilia": "Herps",
    "Amphibia": "Herps",
    "Arachnida": "Arachnida",
}
BASELINE_DAYS = 60
RECENT_PAGE_SIZE = 200
BASELINE_PAGE_SIZE = 500
BASELINE_MAX_PAGES = 3

def _pretty(name: str) -> str:
    """'rose-ringed parakeet' -> 'Rose-ringed Parakeet' (without str.title()'s apostrophe bugs)."""
    small = {"and", "of", "the", "in", "on", "or", "to", "a"}
    words = name.split(" ")
    return " ".join(w if (i and w.lower() in small) else w[:1].upper() + w[1:] for i, w in enumerate(words))

def _species(result: dict) -> dict | None:
    taxon = result.get("taxon") or {}
    if not taxon.get("id") or not taxon.get("name"):
        return None
    group = _ICONIC_TO_GROUP.get(taxon.get("iconic_taxon_name") or "", "Other")
    label, _emoji = _GROUP_META[group]
    common = taxon.get("preferred_common_name")
    common = _pretty(common.strip()) if isinstance(common, str) and common.strip() else None
    photo = taxon.get("default_photo") or {}
    return {
        "id": taxon["id"],
        "sci": taxon["name"],
        "common": common,
        "name": common or taxon["name"],
        "group": group,
        "group_label": label,
        "count": int(result.get("count") or 0),
        "fresh": False,
        "photo": photo.get("medium_url") or photo.get("square_url"),
        "photo_credit": photo.get("attribution"),
        "url": f"https://www.inaturalist.org/taxa/{taxon['id']}",
        "wiki": taxon.get("wikipedia_url"),
    }

def _params(lat: float, lng: float, radius_km: int, d1: dt.date, d2: dt.date, per_page: int, page: int = 1) -> dict:
    return {
        # ~1 km precision is plenty for a 5-50 km radius, improves cache hits,
        # and means we never send anyone's exact position to a third party.
        "lat": round(lat, 2),
        "lng": round(lng, 2),
        "radius": radius_km,
        "d1": d1.isoformat(),
        "d2": d2.isoformat(),
        "quality_grade": "research",  # community-confirmed IDs only
        "verifiable": "true",
        "captive": "false",  # skip zoo animals and garden plantings
        "per_page": per_page,
        "page": page,
        "locale": "en",
    }

def _recent_species(lat, lng, radius_km, d1, d2) -> tuple[list[dict], int]:
    data = http.get_json(
        SPECIES_COUNTS_URL, _params(lat, lng, radius_km, d1, d2, RECENT_PAGE_SIZE), ttl=3600, timeout=20
    )
    species = [s for s in (_species(r) for r in data.get("results") or []) if s]
    species.sort(key=lambda s: (-s["count"], s["name"]))
    total = data.get("total_results")
    return species, int(total) if isinstance(total, int) else len(species)


def _baseline_ids(lat, lng, radius_km, d1, d2) -> set[int] | None:
    """Taxon ids seen in the baseline window, or ``None`` if we couldn't get the full list."""
    ids: set[int] = set()
    total = None
    for page in range(1, BASELINE_MAX_PAGES + 1):
        data = http.get_json(
            SPECIES_COUNTS_URL,
            _params(lat, lng, radius_km, d1, d2, BASELINE_PAGE_SIZE, page),
            ttl=6 * 3600,
            timeout=25,
        )
        rows = data.get("results") or []
        total = data.get("total_results", total)
        if not rows:
            break
        ids.update(r["taxon"]["id"] for r in rows if (r.get("taxon") or {}).get("id"))
        if isinstance(total, int) and len(ids) >= total:
            break
    if isinstance(total, int) and len(ids) < total:
        return None  # incomplete baseline -> "new" flags would be unreliable
    return ids

def _observation_total(lat, lng, radius_km, d1, d2) -> int | None:
    try:
        data = http.get_json(
            OBSERVATIONS_URL, _params(lat, lng, radius_km, d1, d2, per_page=0), ttl=3600, timeout=20
        )
    except http.UpstreamError:
        return None
    total = data.get("total_results")
    return total if isinstance(total, int) else None

def fetch_recent(
    lat: float,
    lng: float,
    radius_km: int = 10,
    days: int = 14,
    today: dt.date | None = None,
    top_per_group: int = 6,
) -> dict:
    today = today or dt.date.today()
    d1 = today - dt.timedelta(days=days)
    species, total_species = _recent_species(lat, lng, radius_km, d1, today)
    fresh_known = False
    if species:
        try:
            seen_before = _baseline_ids(
                lat, lng, radius_km, d1 - dt.timedelta(days=BASELINE_DAYS), d1 - dt.timedelta(days=1)
            )
        except http.UpstreamError:
            seen_before = None
        if seen_before is not None:
            fresh_known = True
            for s in species:
                s["fresh"] = s["id"] not in seen_before
    by_group: dict[str, list[dict]] = {}
    for s in species:
        by_group.setdefault(s["group"], []).append(s)
    groups = [
        {"key": key, "label": label, "emoji": emoji, "species": by_group[key][:top_per_group]}
        for key, label, emoji in GROUPS
        if key in by_group
    ]
    fresh = [s for s in species if s["fresh"]]
    observations = _observation_total(lat, lng, radius_km, d1, today) if species else 0
    return {
        "window": {"from": d1.isoformat(), "to": today.isoformat()},
        "totals": {"species": total_species, "observations": observations, "fresh": len(fresh)},
        "fresh_known": fresh_known,
        "top": species[:8],
        "fresh": fresh[:10],
        "groups": groups,
    }
