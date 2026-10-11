# Local open-weight model integration (Ollama by default, or any OpenAI-compatible server).

from __future__ import annotations
import json
import os
import re
from typing import Any
import requests

GROUP_TIPS = {
    "Aves": "Go early or near dusk and listen first — most birds are heard before they're seen.",
    "Plantae": "Look at leaf shape, flower colour and where it grows. A close look beats a quick glance.",
    "Fungi": "Look, don't pick. Check under logs and leaf litter after rain, and photograph the underside.",
    "Insecta": "Try sunny flowers and sheltered spots on a warm afternoon.",
    "Mammalia": "Dawn and dusk are best. Look for tracks and droppings near water.",
    "Herps": "Warm, sunny rocks and damp edges. Move slowly and let them come to you.",
    "Arachnida": "Check webs in dewy early light and under bark or stones — look, don't touch.",
    "Other": "Slow down and scan the ground, the bark and the water's edge.",
}

SYSTEM_PROMPT = (
    "You write the text for a one-page printable 'field guide card' that nudges people to go outside. "
    "STRICT RULES: use ONLY the species, counts and weather provided. Never invent species, places, "
    "sightings or statistics. Tips must be general, well-established hints about how to recognise or find "
    "that species (what to look or listen for, where it hangs out). If you are not sure about a species, "
    "give a general tip for its group instead. Never say anything about eating, tasting, cooking, foraging, "
    "or whether something is safe, toxic or poisonous. Tone: warm, playful, concise. No emojis. "
    "Reply with ONE JSON object and nothing else, with exactly these keys: "
    '"intro" (at most 2 sentences), '
    '"mission" (one sentence: a small outdoor challenge for this week using the listed species), '
    '"tips" (an object mapping each scientific_name you were given to ONE short sentence of at most 18 words).'
)
_UNSAFE = re.compile(
    r"\b(edible|inedible|safe to|harmless|non-?toxic|taste|tasting|forag\w*|cook\w*|recipe|"
    r"eat (it|them|this|these)|(is|are) (not )?(poisonous|toxic|venomous))\b",
    re.IGNORECASE,
)

# settings / status

def _base(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    return url.replace("://0.0.0.0", "://localhost")

def settings() -> dict:
    backend = os.getenv("LLM_BACKEND", "ollama").strip().lower()
    if backend not in ("ollama", "openai"):
        backend = "ollama"
    if backend == "openai":
        base = _base(os.getenv("LLM_BASE_URL", "http://localhost:8080"))
        model = os.getenv("LLM_MODEL", "local-model")
    else:
        base = _base(os.getenv("OLLAMA_HOST", "http://localhost:11434"))
        model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    try:
        timeout = float(os.getenv("LLM_TIMEOUT", "120"))
    except ValueError:
        timeout = 120.0
    return {"backend": backend, "base": base, "model": model, "timeout": timeout}

def status() -> dict:
    """Is a local model reachable right now? (Fast: 2-second timeout.)"""
    s = settings()
    out = {"available": False, "backend": s["backend"], "model": s["model"], "hint": ""}
    try:
        if s["backend"] == "openai":
            r = requests.get(s["base"] + "/v1/models", timeout=2)
            r.raise_for_status()
            out["available"] = True
        else:
            r = requests.get(s["base"] + "/api/tags", timeout=2)
            r.raise_for_status()
            names = {m.get("name") for m in (r.json().get("models") or [])}
            if s["model"] in names or f"{s['model']}:latest" in names:
                out["available"] = True
            else:
                out["hint"] = f"Model not installed. Run: ollama pull {s['model']}"
    except (requests.RequestException, ValueError):
        out["hint"] = "Can't reach the local model server. Start it (for example `ollama serve`) to enable AI text."
    return out


# calling the model

def _chat(system: str, user: str) -> str:
    s = settings()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if s["backend"] == "openai":
        body = {
            "model": s["model"],
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": 900,
            "response_format": {"type": "json_object"},
        }
        r = requests.post(s["base"] + "/v1/chat/completions", json=body, timeout=s["timeout"])
        if r.status_code == 400:  # some servers reject response_format; retry without it
            body.pop("response_format")
            r = requests.post(s["base"] + "/v1/chat/completions", json=body, timeout=s["timeout"])
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    body = {
        "model": s["model"],
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.4, "num_predict": 900},
    }
    r = requests.post(s["base"] + "/api/chat", json=body, timeout=s["timeout"])
    r.raise_for_status()
    return r.json()["message"]["content"]

def _user_prompt(ctx: dict, candidates: list[dict]) -> str:
    payload = {
        "place": ctx.get("place") or "your area",
        "window_days": ctx["days"],
        "radius_km": ctx["radius_km"],
        "species_logged": ctx["totals"]["species"],
        "best_weather_day": ctx.get("best_day"),
        "species": [
            {
                "scientific_name": c["sci"],
                "common_name": c["common"],
                "group": c["group_label"],
                "sightings": c["count"],
                "new_lately": c["fresh"],
            }
            for c in candidates
        ],
    }
    return "Write the card text for this data:\n" + json.dumps(payload, ensure_ascii=False)


# parsing / validation

def _extract_json(text: str) -> Any:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("model reply contained no JSON object")

def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    s = re.sub(r"\s+", " ", value).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return s

def _tips(raw: Any, known: set[str]) -> dict[str, str]:
    lookup = {k.lower(): k for k in known}
    pairs: list[tuple[Any, Any]] = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):  # some small models return [{"name": ..., "tip": ...}]
        for item in raw:
            if isinstance(item, dict):
                name = item.get("scientific_name") or item.get("name") or item.get("species")
                pairs.append((name, item.get("tip") or item.get("text")))
    out: dict[str, str] = {}
    for name, tip in pairs:
        key = lookup.get(str(name or "").strip().lower())  # drops species the model made up
        text = _text(tip, 140)
        if key and text and not _UNSAFE.search(text):
            out[key] = text
    return out

def _clean(data: Any, known: set[str], fallback: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("model reply was not a JSON object")
    intro = _text(data.get("intro"), 320)
    mission = _text(data.get("mission"), 220)
    tips = _tips(data.get("tips"), known)
    if not (intro or mission or tips):
        raise ValueError("model reply was empty")
    if not intro or _UNSAFE.search(intro):
        intro = fallback["intro"]
    if not mission or _UNSAFE.search(mission):
        mission = fallback["mission"]
    return {"intro": intro, "mission": mission, "tips": tips}

# public API

def template_narration(ctx: dict) -> dict:
    """Deterministic text built straight from the data. Used when no model is available."""
    totals = ctx["totals"]
    top = ctx.get("top") or []
    fresh = ctx.get("fresh") or []
    if totals["species"]:
        bits = [f"In the last {ctx['days']} days, people within {ctx['radius_km']} km logged {totals['species']:,} species."]
        if top:
            bits.append(f"{top[0]['name']} was the most-reported sighting.")
        if ctx.get("fresh_known") and totals["fresh"]:
            bits.append(f"{totals['fresh']:,} species showed up that nobody logged in the two months before.")
        intro = " ".join(bits)
    else:
        intro = "Nobody has logged sightings nearby lately. Try a bigger radius or a longer window."
    if fresh:
        mission = f"Find {min(3, len(fresh))} of the “just showed up” species — start by looking for {fresh[0]['name']}."
    elif top:
        mission = f"Spot three different species from this card — {top[0]['name']} is a good first target."
    else:
        mission = "Walk somewhere green for twenty minutes and write down five things you notice."

    return {"intro": intro, "mission": mission, "tips": {}, "source": "template"}

def narrate(ctx: dict, candidates: list[dict]) -> dict:
    """Ask the local model for card text; fall back to the template on any problem."""
    fallback = template_narration(ctx)
    try:
        raw = _chat(SYSTEM_PROMPT, _user_prompt(ctx, candidates))
        cleaned = _clean(_extract_json(raw), {c["sci"] for c in candidates}, fallback)
        s = settings()
        cleaned["source"] = f"{s['backend']}:{s['model']}"
        return cleaned
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
        fallback["fallback_reason"] = f"{type(exc).__name__}: {exc}"[:200]
        return fallback
