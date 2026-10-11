# 🍂 This Week Outside

**A hyperlocal field guide you print and take outdoors.**
Type where you live → get a one-page "things to look for this week" card built from what your neighbours actually spotted in the last two weeks, plus the forecast and a small AI model that runs on **your own machine** to write the words. Print it, leave the phone at home, go look.

Built for the [Hacktoberfest Open-Source AI Challenge - Week 1: Touch Grass](https://dev.to/challenges/hacktoberfest-week1-2026-10-05).

---

## What you get

- **The card** : species count, "just showed up" arrivals, the top sightings per group (birds, plants, fungi, insects, mammals, reptiles & amphibians, spiders…), and a one-line weekly mission.
- **Best window outside** : 7-day outlook with a ★ on the best day and golden-hour time.
- **A print stylesheet** : prints only the field card, with clear medium-resolution photos and a paginated, ink-friendly layout. The whole sightings list flows across pages without hiding species.
- **Local AI text** : an open-weight model (default `qwen2.5:3b` via Ollama) writes the intro, the mission and short "what to look/listen for" tips. If no model is running the app still works, with template text.
- **Tick boxes** on the printed card (and clickable on screen, remembered in your browser), a ruled "field notes" area, copy as text, °C/°F toggle, and "not the right place?" suggestions.
- **Sample-data mode** : one click ("Try with sample data") runs the whole pipeline with built-in illustrative sightings, no internet needed. Cards made this way are clearly stamped *not real sightings*.

## Quick start

Requires Python 3.10+.

```bash
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows PowerShell:
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000> in your browser. The app works with template text without Ollama; live sightings and weather need an internet connection.

To turn on local AI text:

```bash
# install Ollama from https://ollama.com, then:
ollama pull qwen2.5:3b
ollama serve            # if it isn't already running
```

Reload the page: the header pill turns green (`AI · qwen2.5:3b · local`).

To try everything with no internet at all (or to demo it), start the server in sample-data mode:

```bash
# macOS / Linux:
TWO_DEMO=1 python app.py
# Windows PowerShell:
$env:TWO_DEMO = "1"; python app.py
```
## How it works

```
 browser ──► Flask (app.py) ──► fieldguide/guide.py
                                   ├─ inat.py     iNaturalist species_counts  (recent window + 60-day baseline → "just showed up")
                                   ├─ weather.py  Open-Meteo 7-day forecast   (+ best-day score, golden hour)
                                   ├─ geocode.py  Open-Meteo geocoding         (place name → lat/lng)
                                   └─ llm.py      local model via Ollama / OpenAI-compatible API
```
