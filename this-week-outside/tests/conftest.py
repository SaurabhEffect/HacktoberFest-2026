"""Shared fixtures: a fake network that mimics the shapes of the real iNaturalist / Open-Meteo APIs."""

from __future__ import annotations
import datetime as dt
import pytest
from fieldguide import geocode, http, inat, weather
TODAY = dt.date.today()

def result(id_, sci, common, iconic, count, photo=True):
    taxon = {
        "id": id_,
        "name": sci,
        "rank": "species",
        "preferred_common_name": common,
        "iconic_taxon_name": iconic,
        "wikipedia_url": f"https://en.wikipedia.org/wiki/{sci.replace(' ', '_')}",
    }
    if photo:
        taxon["default_photo"] = {
            "square_url": f"https://inaturalist-open-data.s3.amazonaws.com/photos/{id_}/square.jpg",
            "attribution": "(c) Someone, some rights reserved (CC BY)",
        }
    return {"count": count, "taxon": taxon}

RECENT = [
    result(1, "Psittacula krameri", "rose-ringed parakeet", "Aves", 34),
    result(2, "Corvus splendens", "House Crow", "Aves", 20),
    result(3, "Tagetes erecta", "Mexican marigold", "Plantae", 12),
    result(5, "Danaus chrysippus", "Plain Tiger", "Insecta", 9),
    result(4, "Agaricus campestris", "Field Mushroom", "Fungi", 3),
    result(6, "Ptyas mucosa", "Oriental Rat Snake", "Reptilia", 2),
    result(7, "Hoplobatrachus tigerinus", "Indian Bullfrog", "Amphibia", 1, photo=False),
    result(8, "Argiope anasuja", None, "Arachnida", 1),
]

BASELINE = [
    result(1, "Psittacula krameri", "rose-ringed parakeet", "Aves", 80),
    result(2, "Corvus splendens", "House Crow", "Aves", 60),
    result(3, "Tagetes erecta", "Mexican marigold", "Plantae", 20),
    result(5, "Danaus chrysippus", "Plain Tiger", "Insecta", 15),
]
WEATHER = {
    "timezone": "Asia/Kolkata",
    "daily": {
        "time": [f"2026-10-{d}" for d in range(10, 17)],
        "weather_code": [3, 61, 0, 95, 2, 1, None],
        "temperature_2m_max": [31, 24, 23, 29, 36, 25, None],
        "temperature_2m_min": [20, 19, 17, 21, 24, 16, None],
        "precipitation_probability_max": [20, 80, 5, 90, 10, 10, None],
        "wind_speed_10m_max": [12, 25, 8, 40, 15, 10, None],
        "sunrise": [f"2026-10-{d}T06:12" for d in range(10, 16)] + [None],
        "sunset": [f"2026-10-{d}T17:48" for d in range(10, 16)] + [None],
    },
}
GEO = {
    "results": [
        {"name": "Pune", "admin1": "Maharashtra", "country": "India", "latitude": 18.52, "longitude": 73.86, "timezone": "Asia/Kolkata"},
        {"name": "Pune", "admin1": "Odisha", "country": "India", "latitude": 20.1, "longitude": 85.1, "timezone": "Asia/Kolkata"},
        {"name": "Broken", "country": "Nowhere", "latitude": None, "longitude": None},
    ]
}

class FakeNet:
    """Callable replacement for ``fieldguide.http.get_json``."""
    def __init__(self, today=TODAY):
        self.today = today
        self.calls: list[tuple[str, dict]] = []
        self.recent = RECENT
        self.baseline = BASELINE
        self.baseline_total = None  # override to simulate a truncated baseline
        self.obs_total = 321
        self.fail: set[str] = set()  # URL substrings that should raise UpstreamError
    def __call__(self, url, params=None, ttl=900, timeout=12):
        params = dict(params or {})
        self.calls.append((url, params))
        if any(f in url for f in self.fail):
            raise http.UpstreamError(f"{url} failed")
        if url == inat.SPECIES_COUNTS_URL:
            is_recent = params["d2"] == self.today.isoformat()
            rows = self.recent if is_recent else self.baseline
            total = len(rows) if is_recent or self.baseline_total is None else self.baseline_total
            if int(params.get("page", 1)) > 1:
                rows = []
            return {"total_results": total, "page": params.get("page", 1), "per_page": params["per_page"], "results": rows}
        if url == inat.OBSERVATIONS_URL:
            return {"total_results": self.obs_total, "results": []}
        if url == weather.FORECAST_URL:
            return WEATHER
        if url == geocode.GEOCODE_URL:
            return GEO
        raise AssertionError(f"unexpected URL in test: {url}")

    def species_calls(self):
        return [p for u, p in self.calls if u == inat.SPECIES_COUNTS_URL]

@pytest.fixture
def net(monkeypatch):
    fake = FakeNet()
    monkeypatch.setattr(http, "get_json", fake)
    return fake