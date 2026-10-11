# Place-name -> coordinates, via the free Open-Meteo geocoding API .

from __future__ import annotations
from . import http
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

def search_place(query: str, count: int = 5) -> list[dict]:
    query = (query or "").strip()
    if len(query) < 2:
        return []
    data = http.get_json(
        GEOCODE_URL,
        {"name": query, "count": count, "language": "en", "format": "json"},
        ttl=86400,
    )
    places = []
    for r in data.get("results") or []:
        if r.get("latitude") is None or r.get("longitude") is None:
            continue
        parts = [r.get("name"), r.get("admin1"), r.get("country")]
        label = ", ".join(dict.fromkeys(p for p in parts if p))  # dedupe, keep order
        places.append(
            {
                "name": r.get("name") or label,
                "label": label,
                "lat": float(r["latitude"]),
                "lng": float(r["longitude"]),
                "timezone": r.get("timezone"),
            }
        )
    return places