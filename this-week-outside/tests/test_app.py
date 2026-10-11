import pytest
import app as webapp
from fieldguide import llm

@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()

def test_index_and_static_assets_are_served(client):
    r = client.get("/")
    assert r.status_code == 200 and b"This Week" in r.data
    for path in ("/static/app.js", "/static/style.css", "/static/favicon.svg"):
        assert client.get(path).status_code == 200, path
    assert r.headers["X-Content-Type-Options"] == "nosniff"

def test_health_reports_ai_status(client, monkeypatch):
    monkeypatch.setattr(llm, "status", lambda: {"available": True, "backend": "ollama", "model": "qwen2.5:3b", "hint": ""})
    body = client.get("/api/health").get_json()
    assert body["ok"] is True and body["ai"]["model"] == "qwen2.5:3b"

def test_geocode_returns_places_and_skips_broken_rows(client, net):
    body = client.get("/api/geocode?q=Pune").get_json()
    assert [p["label"] for p in body["results"]] == ["Pune, Maharashtra, India", "Pune, Odisha, India"]
    assert body["results"][0]["lat"] == 18.52

def test_geocode_requires_a_query(client):
    r = client.get("/api/geocode?q=a")
    assert r.status_code == 400 and "two characters" in r.get_json()["error"]

def test_guide_happy_path_without_llm(client, net):
    r = client.get("/api/guide?lat=28.61&lng=77.21&radius=10&days=14&place=Delhi&llm=0")
    assert r.status_code == 200
    body = r.get_json()
    assert body["place"] == "Delhi" and body["radius_km"] == 10 and body["days"] == 14
    assert body["totals"]["species"] == 8 and body["fresh_known"] is True
    assert [g["key"] for g in body["groups"]][0] == "Aves"
    assert body["weather"]["best_index"] == 2
    assert body["narration"]["source"] == "template"
    assert set(body["group_tips"]) == {g["key"] for g in body["groups"]}

def test_guide_uses_llm_when_requested(client, net, monkeypatch):
    seen = {}
    def fake_narrate(ctx, candidates):
        seen["ctx"], seen["candidates"] = ctx, candidates
        return {"intro": "AI intro", "mission": "AI mission", "tips": {}, "source": "ollama:test"}
    monkeypatch.setattr(llm, "narrate", fake_narrate)
    body = client.get("/api/guide?lat=1&lng=2&llm=1").get_json()
    assert body["narration"]["source"] == "ollama:test"
    assert seen["ctx"]["best_day"]["weekday"] == "Monday"
    assert 0 < len(seen["candidates"]) <= 24
    assert len({c["sci"] for c in seen["candidates"]}) == len(seen["candidates"])  # no duplicates

def test_guide_skips_llm_when_nothing_was_spotted(client, net, monkeypatch):
    net.recent = []
    monkeypatch.setattr(llm, "narrate", lambda *a: pytest.fail("LLM must not run on an empty area"))
    body = client.get("/api/guide?lat=1&lng=2&llm=1").get_json()
    assert body["groups"] == [] and body["narration"]["source"] == "template"

def test_guide_survives_forecast_outage(client, net):
    net.fail = {"open-meteo"}
    body = client.get("/api/guide?lat=1&lng=2&llm=0").get_json()
    assert body["weather"] is None and body["totals"]["species"] == 8

def test_guide_reports_502_when_inaturalist_is_down(client, net):
    net.fail = {"inaturalist"}
    r = client.get("/api/guide?lat=1&lng=2&llm=0")
    assert r.status_code == 502 and "error" in r.get_json()

@pytest.mark.parametrize(
    "query",
    [
        "",
        "lat=1",
        "lat=abc&lng=2",
        "lat=91&lng=0",
        "lat=0&lng=181",
        "lat=nan&lng=0",
        "lat=1&lng=2&radius=1.5",
        "lat=1&lng=2&days=3.5",
    ],
)
def test_guide_validates_coordinates(client, net, query):
    r = client.get("/api/guide?" + query)
    assert r.status_code == 400 and "error" in r.get_json()

def test_guide_clamps_radius_and_days(client, net):
    body = client.get("/api/guide?lat=1&lng=2&radius=9999&days=1&llm=0").get_json()
    assert body["radius_km"] == 50 and body["days"] == 3