from __future__ import annotations
import datetime as dt
from . import http, inat, llm, weather

def _tip_candidates(sightings: dict, per_group: int = 3, fresh_n: int = 4, cap: int = 24) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    pool = [s for g in sightings["groups"] for s in g["species"][:per_group]] + sightings["fresh"][:fresh_n]
    for s in pool:
        if s["sci"] not in seen:
            seen.add(s["sci"])
            out.append(s)
    return out[:cap]

def build_guide(
    lat: float,
    lng: float,
    radius_km: int = 10,
    days: int = 14,
    place: str = "",
    use_llm: bool = True,
    today: dt.date | None = None,
    demo: bool = False,
) -> dict:
    with http.demo_scope(demo):
        return _build(lat, lng, radius_km, days, place, use_llm, today)

def _strip_links(sightings: dict) -> None:
    for s in {id(s): s for g in sightings["groups"] for s in g["species"]}.values():
        s["url"] = s["photo"] = s["photo_credit"] = s["wiki"] = None
    for s in sightings["top"] + sightings["fresh"]:
        s["url"] = s["photo"] = s["photo_credit"] = s["wiki"] = None

def _build(lat, lng, radius_km, days, place, use_llm, today) -> dict:
    is_demo = http.demo_active()
    sightings = inat.fetch_recent(lat, lng, radius_km=radius_km, days=days, today=today)
    if is_demo:
        _strip_links(sightings)
    try:
        wx = weather.get_week(lat, lng)
    except http.UpstreamError:
        wx = None  # the card is still useful without a forecast
    best_day = None
    if wx and wx.get("best_index") is not None:
        d = wx["days"][wx["best_index"]]
        best_day = {
            "weekday": d["weekday_long"],
            "summary": d["summary"],
            "tmax_c": d["tmax_c"],
            "rain_pct": d["rain_pct"],
        }
    ctx = {
        "place": place,
        "days": days,
        "radius_km": radius_km,
        "totals": sightings["totals"],
        "fresh_known": sightings["fresh_known"],
        "top": sightings["top"],
        "fresh": sightings["fresh"],
        "best_day": best_day,
    }

    if use_llm and sightings["totals"]["species"]:
        narration = llm.narrate(ctx, _tip_candidates(sightings))
    else:
        narration = llm.template_narration(ctx)
    present = {g["key"] for g in sightings["groups"]}
    return {
        "place": place,
        "lat": round(lat, 2),
        "lng": round(lng, 2),
        "radius_km": radius_km,
        "days": days,
        "demo": is_demo,
        **sightings,
        "weather": wx,
        "narration": narration,
        "group_tips": {k: v for k, v in llm.GROUP_TIPS.items() if k in present},
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }