import json
import pytest
import requests
from fieldguide import llm
CTX = {
    "place": "Pune, Maharashtra, India",
    "days": 14,
    "radius_km": 10,
    "totals": {"species": 8, "observations": 321, "fresh": 2},
    "fresh_known": True,
    "top": [{"name": "Rose-ringed Parakeet"}],
    "fresh": [{"name": "Field Mushroom"}, {"name": "Oriental Rat Snake"}],
    "best_day": {"weekday": "Monday", "summary": "Clear", "tmax_c": 23, "rain_pct": 5},
}
CANDIDATES = [
    {"sci": "Psittacula krameri", "common": "Rose-ringed Parakeet", "group_label": "Birds", "count": 34, "fresh": False},
    {"sci": "Agaricus campestris", "common": "Field Mushroom", "group_label": "Fungi", "count": 3, "fresh": True},
]

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
    def json(self):
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

def ollama_reply(obj):
    return FakeResp({"message": {"content": obj if isinstance(obj, str) else json.dumps(obj)}})

GOOD = {
    "intro": "Autumn is waking up around Pune.",
    "mission": "Find the parakeets before breakfast.",
    "tips": {"Psittacula krameri": "Listen for loud, squawking flocks at dusk."},
}

@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("LLM_BACKEND", "OLLAMA_HOST", "OLLAMA_MODEL", "LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)

# parsing

def test_extract_json_handles_code_fences_and_chatter():
    assert llm._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm._extract_json('Sure! Here you go: {"a": 1} Hope that helps') == {"a": 1}
    with pytest.raises(ValueError):
        llm._extract_json("no json here")

def test_clean_drops_invented_species_and_unsafe_claims():
    fallback = llm.template_narration(CTX)
    raw = {
        "intro": "Hello.",
        "mission": "Go.",
        "tips": {
            "Psittacula krameri": "Listen for loud flocks.",
            "Unicornus magicus": "Totally made up species.",             # not in the data -> dropped
            "Agaricus campestris": "A tasty edible mushroom.",             # edibility claim -> dropped
        },
    }
    out = llm._clean(raw, {"Psittacula krameri", "Agaricus campestris"}, fallback)
    assert out["tips"] == {"Psittacula krameri": "Listen for loud flocks."}

def test_clean_accepts_list_style_tips_and_is_case_insensitive():
    out = llm._clean(
        {"intro": "Hi.", "mission": "Go.", "tips": [{"name": "psittacula KRAMERI", "tip": "Look up."}]},
        {"Psittacula krameri"},
        llm.template_narration(CTX),
    )
    assert out["tips"] == {"Psittacula krameri": "Look up."}

def test_unsafe_intro_or_mission_is_replaced_by_template():
    fallback = llm.template_narration(CTX)
    out = llm._clean({"intro": "These berries are safe to eat!", "mission": "Go cook the mushrooms."}, set(), fallback)
    assert out["intro"] == fallback["intro"]
    assert out["mission"] == fallback["mission"]

def test_clean_truncates_long_text_and_rejects_empty():
    out = llm._clean({"intro": "word " * 200, "mission": "ok"}, set(), llm.template_narration(CTX))
    assert len(out["intro"]) <= 321 and out["intro"].endswith("…")
    with pytest.raises(ValueError):
        llm._clean({}, set(), llm.template_narration(CTX))
    with pytest.raises(ValueError):
        llm._clean(["not", "a", "dict"], set(), llm.template_narration(CTX))


# narrate()

def test_narrate_ollama_success(monkeypatch):
    seen = {}
    def fake_post(url, json=None, timeout=None):
        seen.update(url=url, body=json, timeout=timeout)
        return ollama_reply(GOOD)
    monkeypatch.setattr(llm.requests, "post", fake_post)
    out = llm.narrate(CTX, CANDIDATES)
    assert out["source"] == "ollama:qwen2.5:3b"
    assert out["intro"] == GOOD["intro"] and out["tips"] == GOOD["tips"]
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["body"]["format"] == "json" and seen["body"]["stream"] is False
    prompt = seen["body"]["messages"][1]["content"]
    assert "Psittacula krameri" in prompt and "Pune" in prompt

def test_narrate_falls_back_when_server_is_down(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(llm.requests, "post", boom)
    out = llm.narrate(CTX, CANDIDATES)
    assert out["source"] == "template"
    assert "ConnectionError" in out["fallback_reason"]
    assert out["intro"] and out["mission"]

def test_narrate_falls_back_on_garbage_reply(monkeypatch):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: ollama_reply("I cannot do that, Dave."))
    out = llm.narrate(CTX, CANDIDATES)
    assert out["source"] == "template"
    assert out["fallback_reason"].startswith("ValueError")

def test_narrate_openai_backend_and_response_format_retry(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "localhost:1234/")
    monkeypatch.setenv("LLM_MODEL", "gemma-3-4b")
    calls = []
    def fake_post(url, json=None, timeout=None):
        calls.append((url, dict(json)))
        if "response_format" in json:
            return FakeResp({}, status=400)  # e.g. LM Studio rejecting json_object
        return FakeResp({"choices": [{"message": {"content": json_module.dumps(GOOD)}}]})
    import json as json_module
    monkeypatch.setattr(llm.requests, "post", fake_post)
    out = llm.narrate(CTX, CANDIDATES)
    assert out["source"] == "openai:gemma-3-4b"
    assert len(calls) == 2 and "response_format" not in calls[1][1]
    assert calls[0][0] == "http://localhost:1234/v1/chat/completions"

# settings / status

def test_settings_normalise_host_values(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    assert llm.settings()["base"] == "http://localhost:11434"
    monkeypatch.setenv("LLM_BACKEND", "nonsense")
    assert llm.settings()["backend"] == "ollama"
    monkeypatch.setenv("LLM_TIMEOUT", "abc")
    assert llm.settings()["timeout"] == 120.0

def test_status_unreachable(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("nope")
    monkeypatch.setattr(llm.requests, "get", boom)
    st = llm.status()
    assert st["available"] is False and "ollama serve" in st["hint"]

def test_status_model_present_and_missing(monkeypatch):
    monkeypatch.setattr(llm.requests, "get", lambda *a, **k: FakeResp({"models": [{"name": "qwen2.5:3b"}]}))
    assert llm.status()["available"] is True
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2")
    st = llm.status()
    assert st["available"] is False and "ollama pull llama3.2" in st["hint"]


# template

def test_template_narration_mentions_real_numbers():
    out = llm.template_narration(CTX)
    assert "8 species" in out["intro"] and "Rose-ringed Parakeet" in out["intro"]
    assert "Field Mushroom" in out["mission"]
    assert out["source"] == "template" and out["tips"] == {}

def test_template_narration_for_empty_area():
    ctx = {**CTX, "totals": {"species": 0, "observations": 0, "fresh": 0}, "top": [], "fresh": []}
    out = llm.template_narration(ctx)
    assert "bigger radius" in out["intro"]
    assert "twenty minutes" in out["mission"]