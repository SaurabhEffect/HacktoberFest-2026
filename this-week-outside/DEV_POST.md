---
title: This Week Outside — a printable field guide from open data and a local LLM
published: false
tags: devchallenge, hf26challenge
---

*This is a submission for the [Hacktoberfest Open-Source AI Challenge Week 1: Touch Grass](https://dev.to/challenges/hacktoberfest-week1-2026-10-05)*

## What I Built

**This Week Outside** turns "what should I look for outside?" into a one-page card you print and take with you.

You type your town. It pulls what people within a few kilometres have actually spotted on iNaturalist in the last two weeks, checks the 7-day forecast, and flags species that are *new* here lately (logged recently, but not in the previous two months). A small open-weight model running on your own machine then writes the intro, a one-line weekly mission and short "what to look or listen for" tips. You hit print, leave the phone at home, and go find things.

It's for anyone who'd walk more if the walk came with a tiny quest, and who'd rather not stare at an app on the trail to do it.

## Demo

<!-- TODO: add a deployed link and/or a short screen recording. A photo of the printed card in your hand outdoors would be perfect. -->

## Code

<!-- TODO: embed the repo, e.g. {% embed https://github.com/YOUR_USERNAME/this-week-outside %} -->

## How I Built It

- **Open data:** iNaturalist `species_counts` (recent window + a 60-day baseline to detect "just showed up") and Open-Meteo for the forecast and geocoding. No API keys.
- **Open-weight model:** `qwen2.5:3b` through Ollama by default; any OpenAI-compatible local server (llama.cpp, LM Studio, vLLM) works via two environment variables.
- **Data decides, the model decorates:** the species on the card come straight from iNaturalist. The model only writes the words around them, and its JSON output is validated: tips for species that aren't in the data are discarded, and any tip that makes an edibility or toxicity claim is dropped. If the model is down or returns junk, deterministic template text takes over, so the app always works.
- **Two-pass loading:** the card renders in about a second with template text, then the local model rewrites it in the background and the card updates when it's ready.
- **UI:** inspired by hacktoberfest.com: forest green, cream paper, die-cut SVG stickers, italic-serif accents, numbered steps. No web fonts or third-party requests.
- **Stack:** Flask + `requests`, vanilla JS (no build step, DOM built with text nodes only), a print stylesheet that fits the card on one page, a pytest suite that runs against a fake network, and a built-in sample-data mode so it can be demoed with no internet.

## Why Does Open Innovation Matter?

- **Private by default:** the model runs locally, your coordinates are rounded to ~1 km before leaving your machine, and nothing is stored on a server.
- **Free to run, forever:** open data plus an open-weight model means no key, no bill and no paid tier to outgrow.
- **Swappable:** change `OLLAMA_MODEL` to try another model, or change the prompt to change the voice. A closed API would have fixed both for me.
- **Works where the signal doesn't:** once the card is printed, the trail needs no connection at all.

## I Took It Outside

<!-- TODO (bonus points!): where did you go, what did the card get right, what did it miss, what did you find that wasn't on it? Add photos. Write only what actually happened. -->

## My Agent Session

<!-- Optional: save your session with DevRelay and embed it with the agent_session tag, or link to it. -->

## Prize Categories

<!-- TODO: list every partner category you're entering, or remove this section. -->
