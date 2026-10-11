# Built-in sample data, so the app can be tried (and demoed) with no internet at all.

from __future__ import annotations
import datetime as dt

# (id, scientific name, common name, iconic taxon, recent sightings, also seen in the earlier baseline?)
# ids are placeholders (not real iNaturalist taxon ids) — links and photos are stripped in demo mode.
_ROWS = [
    # Birds
    (900001, "Turdus migratorius", "American robin", "Aves", 46, True),
    (900002, "Cyanocitta cristata", "blue jay", "Aves", 31, True),
    (900003, "Junco hyemalis", "dark-eyed junco", "Aves", 24, False),
    (900004, "Cardinalis cardinalis", "northern cardinal", "Aves", 22, True),
    (900005, "Poecile atricapillus", "black-capped chickadee", "Aves", 19, True),
    (900006, "Zonotrichia albicollis", "white-throated sparrow", "Aves", 17, False),
    (900007, "Setophaga coronata", "yellow-rumped warbler", "Aves", 15, False),
    (900008, "Branta canadensis", "Canada goose", "Aves", 14, True),
    # Plants
    (900011, "Acer rubrum", "red maple", "Plantae", 38, True),
    (900012, "Symphyotrichum novae-angliae", "New England aster", "Plantae", 29, True),
    (900013, "Solidago canadensis", "Canada goldenrod", "Plantae", 21, True),
    (900014, "Parthenocissus quinquefolia", "Virginia creeper", "Plantae", 16, True),
    (900015, "Asclepias syriaca", "common milkweed", "Plantae", 11, True),
    (900016, "Rhus typhina", "staghorn sumac", "Plantae", 9, True),
    # Fungi
    (900021, "Trametes versicolor", "turkey tail", "Fungi", 13, True),
    (900022, "Amanita muscaria", "fly agaric", "Fungi", 7, False),
    (900023, "Laetiporus sulphureus", "chicken-of-the-woods", "Fungi", 5, False),
    # Insects
    (900031, "Danaus plexippus", "monarch", "Insecta", 18, True),
    (900032, "Anax junius", "common green darner", "Insecta", 10, True),
    (900033, "Harmonia axyridis", "multicolored Asian lady beetle", "Insecta", 9, True),
    (900034, "Pyrrharctia isabella", "isabella tiger moth", "Insecta", 6, False),
    # Mammals
    (900041, "Sciurus carolinensis", "eastern gray squirrel", "Mammalia", 27, True),
    (900042, "Tamias striatus", "eastern chipmunk", "Mammalia", 12, True),
    (900043, "Odocoileus virginianus", "white-tailed deer", "Mammalia", 8, True),
    # Reptiles & amphibians
    (900051, "Thamnophis sirtalis", "common garter snake", "Reptilia", 6, True),
    (900052, "Lithobates clamitans", "green frog", "Amphibia", 4, True),
    # Spiders
    (900061, "Argiope aurantia", "black and yellow garden spider", "Arachnida", 10, True),
    (900062, "Araneus diadematus", "cross orbweaver", "Arachnida", 5, False),
]

def _taxon(row) -> dict:
    id_, sci, common, iconic, _count, _seen = row
    return {
        "id": id_,
        "name": sci,
        "rank": "species",
        "preferred_common_name": common,
        "iconic_taxon_name": iconic,
    }

def _species_counts(params: dict) -> dict:
    is_recent = params.get("d2") == dt.date.today().isoformat()
    if is_recent:
        rows = [{"count": r[4], "taxon": _taxon(r)} for r in _ROWS]
    else:
        rows = [{"count": r[4] * 3, "taxon": _taxon(r)} for r in _ROWS if r[5]]
    if int(params.get("page", 1)) > 1:
        rows = []
    return {"total_results": sum(1 for _ in (_ROWS if is_recent else [r for r in _ROWS if r[5]])), "results": rows}

def _forecast() -> dict:
    today = dt.date.today()
    dates = [today + dt.timedelta(days=i) for i in range(7)]
    return {
        "timezone": "America/New_York",
        "daily": {
            "time": [d.isoformat() for d in dates],
            "weather_code": [2, 0, 1, 61, 3, 0, 1],
            "temperature_2m_max": [17, 19, 21, 14, 13, 18, 20],
            "temperature_2m_min": [7, 8, 10, 9, 5, 6, 8],
            "precipitation_probability_max": [10, 5, 5, 70, 35, 5, 10],
            "wind_speed_10m_max": [14, 10, 12, 28, 22, 9, 11],
            "sunrise": [f"{d.isoformat()}T06:58" for d in dates],
            "sunset": [f"{d.isoformat()}T18:07" for d in dates],
        },
    }

def get_json(url: str, params: dict | None = None) -> dict:
    """Stand-in for ``fieldguide.http.get_json`` while demo mode is on."""
    from . import geocode, inat, weather  # imported lazily: those modules import http, which imports us
    params = dict(params or {})
    if url == inat.SPECIES_COUNTS_URL:
        return _species_counts(params)
    if url == inat.OBSERVATIONS_URL:
        return {"total_results": sum(r[4] for r in _ROWS) * 2, "results": []}
    if url == weather.FORECAST_URL:
        return _forecast()
    if url == geocode.GEOCODE_URL:
        name = str(params.get("name") or "Sample meadow").strip()[:60] or "Sample meadow"
        return {
            "results": [
                {"name": name, "admin1": "sample data", "latitude": 42.36, "longitude": -71.06, "timezone": "America/New_York"}
            ]
        }
    raise AssertionError(f"demo mode has no data for {url}")
