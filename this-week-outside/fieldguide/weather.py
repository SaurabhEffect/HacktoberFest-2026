"""7-day outlook from Open-Meteo plus a simple "best day to be outside" score."""

from __future__ import annotations
import datetime as dt
from . import http
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_FIELDS = [
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
    "wind_speed_10m_max",
    "sunrise",
    "sunset",
]

# WMO weather interpretation codes -> (label, emoji)
_WMO = {
    0: ("Clear", "☀️"),
    1: ("Mostly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Drizzle", "🌦️"),
    55: ("Heavy drizzle", "🌦️"),
    56: ("Freezing drizzle", "🌧️"),
    57: ("Freezing drizzle", "🌧️"),
    61: ("Light rain", "🌧️"),
    63: ("Rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"),
    67: ("Freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"),
    73: ("Snow", "🌨️"),
    75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Rain showers", "🌦️"),
    81: ("Rain showers", "🌧️"),
    82: ("Violent showers", "🌧️"),
    85: ("Snow showers", "🌨️"),
    86: ("Snow showers", "🌨️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm, hail", "⛈️"),
    99: ("Thunderstorm, hail", "⛈️"),
}

def describe(code: int | None) -> tuple[str, str]:
    if code is None:
        return ("Unknown", "❔")
    return _WMO.get(int(code), ("Mixed weather", "🌡️"))

def outdoor_score(tmax, rain_pct, wind_kmh, code) -> float | None:
    """0-100, higher = nicer to be outside. ``None`` when we lack the basics to judge."""
    if tmax is None or rain_pct is None:
        return None
    score = 100.0
    score -= rain_pct * 0.8
    if tmax > 27:
        score -= (tmax - 27) * 4
    elif tmax < 12:
        score -= (12 - tmax) * 3
    if wind_kmh is not None and wind_kmh > 25:
        score -= (wind_kmh - 25) * 1.5
    if code is not None:
        if code >= 95:
            score -= 40
        elif code in (45, 48):
            score -= 10
        elif code == 3:
            score -= 3
    return round(max(0.0, min(100.0, score)), 1)

def _at(daily: dict, name: str, i: int):
    arr = daily.get(name) or []
    return arr[i] if i < len(arr) else None

def _hhmm(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return None

def _golden_hour(sunset_iso: str | None) -> str | None:
    """Start of the last hour of daylight — the soft light photographers love."""
    if not sunset_iso:
        return None
    try:
        return (dt.datetime.fromisoformat(sunset_iso) - dt.timedelta(hours=1)).strftime("%H:%M")
    except ValueError:
        return None

def get_week(lat: float, lng: float) -> dict:
    """Seven-day outlook for a point. Temperatures are °C, wind km/h (the UI converts units)."""
    data = http.get_json(
        FORECAST_URL,
        {
            "latitude": round(lat, 2),
            "longitude": round(lng, 2),
            "daily": ",".join(DAILY_FIELDS),
            "timezone": "auto",
            "forecast_days": 7,
        },
        ttl=1800,
    )
    daily = data.get("daily") or {}
    days = []
    for i, date_str in enumerate(daily.get("time") or []):
        try:
            date = dt.date.fromisoformat(date_str)
        except ValueError:
            continue
        code = _at(daily, "weather_code", i)
        tmax = _at(daily, "temperature_2m_max", i)
        rain = _at(daily, "precipitation_probability_max", i)
        wind = _at(daily, "wind_speed_10m_max", i)
        sunset = _at(daily, "sunset", i)
        summary, icon = describe(code)
        days.append(
            {
                "date": date_str,
                "weekday": date.strftime("%a"),
                "weekday_long": date.strftime("%A"),
                "code": code,
                "summary": summary,
                "icon": icon,
                "tmax_c": tmax,
                "tmin_c": _at(daily, "temperature_2m_min", i),
                "rain_pct": rain,
                "wind_kmh": wind,
                "sunrise": _hhmm(_at(daily, "sunrise", i)),
                "sunset": _hhmm(sunset),
                "golden_hour": _golden_hour(sunset),
                "score": outdoor_score(tmax, rain, wind, code),
            }
        )

    scored = [i for i, d in enumerate(days) if d["score"] is not None]
    best = max(scored, key=lambda i: (days[i]["score"], -i)) if scored else None
    return {"timezone": data.get("timezone"), "days": days, "best_index": best}
