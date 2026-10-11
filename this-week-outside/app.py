# This Week Outside - Flask server.
from __future__ import annotations
import os
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest, HTTPException
from fieldguide import __version__, geocode, guide, http, llm
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")


# helpers

def _number(name: str, *, lo: float, hi: float, default=None, clamp: bool = False, cast=float):
    raw = request.args.get(name)
    if raw in (None, ""):
        if default is None:
            raise BadRequest(f"Missing required parameter '{name}'.")
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise BadRequest(f"'{name}' must be a number.") from None
    if not (lo <= value <= hi):  # also rejects NaN
        if clamp and value == value:
            return max(lo, min(hi, value))
        raise BadRequest(f"'{name}' must be between {lo:g} and {hi:g}.")
    return value

def _flag(name: str, default: bool = True) -> bool:
    raw = request.args.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# routes

@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "version": __version__, "ai": llm.status(), "demo": http.FORCE_DEMO})

@app.get("/api/geocode")
def api_geocode():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        raise BadRequest("Type at least two characters of a place name.")
    with http.demo_scope(_flag("demo", False)):
        return jsonify({"results": geocode.search_place(q[:100])})

@app.get("/api/guide")
def api_guide():
    lat = _number("lat", lo=-90, hi=90)
    lng = _number("lng", lo=-180, hi=180)
    radius = _number("radius", lo=1, hi=50, default=10, clamp=True, cast=int)
    days = _number("days", lo=3, hi=30, default=14, clamp=True, cast=int)
    place = (request.args.get("place") or "").strip()[:120]
    data = guide.build_guide(lat, lng, radius_km=radius, days=days, place=place, use_llm=_flag("llm", True), demo=_flag("demo", False))
    return jsonify(data)

# errors & headers

@app.errorhandler(HTTPException)
def _http_error(err: HTTPException):
    return jsonify({"error": err.description}), err.code

@app.errorhandler(http.UpstreamError)
def _upstream_error(err: http.UpstreamError):
    return jsonify({"error": str(err)}), 502

@app.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp

if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG") == "1",
    )