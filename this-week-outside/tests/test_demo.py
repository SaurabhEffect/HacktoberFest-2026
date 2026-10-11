"""Demo mode: the full pipeline runs on built-in sample data, with no network at all."""
import pytest
import app as webapp
from fieldguide import guide, http, llm

@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real HTTP attempt in this module is a bug."""
    def boom(*a, **k):
        raise AssertionError("demo mode must not touch the network")
    monkeypatch.setattr(http.requests, "get", boom)
    monkeypatch.setattr(llm, "narrate", lambda ctx, cands: llm.template_narration(ctx))

def test_demo_guide_is_complete_and_labelled(client):
    body = client.get("/api/guide?lat=1&lng=2&place=Anywhere&llm=0&demo=1").get_json()
    assert body["demo"] is True
    assert body["totals"]["species"] >= 20 and body["fresh_known"] is True
    assert {g["key"] for g in body["groups"]} >= {"Aves", "Plantae", "Fungi", "Insecta", "Mammalia", "Herps", "Arachnida"}
    assert 0 < body["totals"]["fresh"] < body["totals"]["species"]
    assert len(body["weather"]["days"]) == 7 and body["weather"]["best_index"] is not None

def test_demo_species_have_no_links_or_photos(client):
    body = client.get("/api/guide?lat=1&lng=2&llm=0&demo=1").get_json()
    every = [s for g in body["groups"] for s in g["species"]] + body["top"] + body["fresh"]
    assert every and all(s["url"] is None and s["photo"] is None and s["wiki"] is None for s in every)

def test_demo_fresh_species_are_the_ones_missing_from_the_baseline(client):
    body = client.get("/api/guide?lat=1&lng=2&llm=0&demo=1").get_json()
    fresh = {s["sci"] for s in body["fresh"]}
    assert "Junco hyemalis" in fresh and "Turdus migratorius" not in fresh

def test_demo_geocode_answers_offline(client):
    body = client.get("/api/geocode?q=Somewhere&demo=1").get_json()
    assert body["results"][0]["name"] == "Somewhere"

def test_demo_scope_does_not_leak_between_requests(client):
    client.get("/api/guide?lat=1&lng=2&llm=0&demo=1")
    assert http.demo_active() is False
    assert guide.build_guide  # real mode would now hit the network and trip the guard above
    with pytest.raises(AssertionError, match="must not touch the network"):
        client.get("/api/guide?lat=1&lng=2&llm=0")

def test_server_wide_demo_flag(client, monkeypatch):
    monkeypatch.setattr(http, "FORCE_DEMO", True)
    body = client.get("/api/guide?lat=1&lng=2&llm=0").get_json()
    assert body["demo"] is True
    monkeypatch.setattr(llm, "status", lambda: {"available": False, "backend": "ollama", "model": "m", "hint": ""})
    assert client.get("/api/health").get_json()["demo"] is True