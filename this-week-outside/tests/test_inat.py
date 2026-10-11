import datetime as dt
from fieldguide import inat
from conftest import TODAY
def fetch(**kw):
    return inat.fetch_recent(28.6139, 77.2090, radius_km=10, days=14, today=TODAY, **kw)

def test_groups_follow_display_order_and_merge_herps(net):
    data = fetch()
    keys = [g["key"] for g in data["groups"]]
    assert keys == ["Aves", "Plantae", "Fungi", "Insecta", "Herps", "Arachnida"]
    herps = next(g for g in data["groups"] if g["key"] == "Herps")
    assert {s["sci"] for s in herps["species"]} == {"Ptyas mucosa", "Hoplobatrachus tigerinus"}

def test_species_sorted_by_count_and_names_prettified(net):
    data = fetch()
    assert [s["count"] for s in data["top"]] == sorted((s["count"] for s in data["top"]), reverse=True)
    assert data["top"][0]["name"] == "Rose-ringed Parakeet"
    assert data["top"][0]["url"] == "https://www.inaturalist.org/taxa/1"

def test_species_without_common_name_falls_back_to_scientific(net):
    data = fetch()
    spider = next(s for g in data["groups"] for s in g["species"] if s["id"] == 8)
    assert spider["common"] is None
    assert spider["name"] == "Argiope anasuja"

def test_species_prefers_print_quality_photo_with_thumbnail_fallback():
    taxon = {
        "id": 1,
        "name": "Example species",
        "default_photo": {"medium_url": "https://example.com/medium.jpg", "square_url": "https://example.com/square.jpg"},
    }
    assert inat._species({"taxon": taxon})["photo"] == "https://example.com/medium.jpg"
    del taxon["default_photo"]["medium_url"]
    assert inat._species({"taxon": taxon})["photo"] == "https://example.com/square.jpg"

def test_fresh_flags_species_missing_from_baseline(net):
    data = fetch()
    assert data["fresh_known"] is True
    assert {s["id"] for s in data["fresh"]} == {4, 6, 7, 8}
    assert data["totals"] == {"species": 8, "observations": 321, "fresh": 4}
    parakeet = data["top"][0]
    assert parakeet["fresh"] is False

def test_incomplete_baseline_disables_fresh_instead_of_guessing(net):
    net.baseline_total = 1000  # API says 1000 species existed, we only ever receive 4
    data = fetch()
    assert data["fresh_known"] is False
    assert data["fresh"] == []
    assert all(not s["fresh"] for g in data["groups"] for s in g["species"])

def test_baseline_failure_is_not_fatal(net):
    class Flaky:
        def __init__(self, inner):
            self.inner = inner
        def __call__(self, url, params=None, **kw):
            if params and params.get("d2") != TODAY.isoformat() and "species_counts" in url:
                from fieldguide import http
                raise http.UpstreamError("baseline down")
            return self.inner(url, params, **kw)
    import fieldguide.http as http
    http.get_json = Flaky(net)  # conftest's monkeypatch fixture restores the original afterwards
    data = fetch()
    assert data["fresh_known"] is False
    assert data["totals"]["species"] == 8

def test_request_parameters_are_privacy_friendly_and_strict(net):
    fetch()
    recent, baseline = net.species_calls()[0], net.species_calls()[-1]
    assert recent["lat"] == 28.61 and recent["lng"] == 77.21  # rounded to ~1 km
    assert recent["quality_grade"] == "research"
    assert recent["captive"] == "false"
    assert recent["radius"] == 10
    assert recent["d1"] == (TODAY - dt.timedelta(days=14)).isoformat()
    assert recent["d2"] == TODAY.isoformat()
    assert baseline["d2"] == (TODAY - dt.timedelta(days=15)).isoformat()  # baseline ends before the recent window
    assert baseline["d1"] == (TODAY - dt.timedelta(days=74)).isoformat()  # 60 days, inclusive

def test_empty_area_returns_empty_but_valid_structure(net):
    net.recent = []
    data = fetch()
    assert data["groups"] == [] and data["top"] == [] and data["fresh"] == []
    assert data["totals"] == {"species": 0, "observations": 0, "fresh": 0}
    assert data["fresh_known"] is False

def test_top_per_group_is_respected(net):
    data = fetch(top_per_group=1)
    assert all(len(g["species"]) == 1 for g in data["groups"])