"""FastAPI app: a small JSON API plus the static single-page UI. Everything runs locally."""
from __future__ import annotations

import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, polish
from .asr import is_model_cached, resolve_device
from .captions import preset_catalog
from .config import LANGUAGES, STATIC_DIR, WHISPER_MODELS, Settings, get_settings
from .exporters import to_ass, to_srt, to_txt, to_vtt
from .jobs import JobError, JobManager, NotFound
from .media import tools_status

DOWNLOADS = {
    "srt": ("application/x-subrip", ".srt"),
    "vtt": ("text/vtt", ".vtt"),
    "ass": ("text/x-ssa", ".ass"),
    "txt": ("text/plain; charset=utf-8", ".txt"),
    "mp4": ("video/mp4", ".mp4"),
}
LANGUAGE_IDS = {item["id"] for item in LANGUAGES}


def _public(manager: JobManager, job: Dict[str, Any]) -> Dict[str, Any]:
    job_id = job["id"]
    task = dict(job.get("task") or {})
    task["url"] = f"/api/jobs/{job_id}/download/mp4" if task.get("file") and task.get("status") == "done" else ""
    options = job.get("options") or {}
    return {
        "id": job_id, "name": job["name"], "status": job["status"], "stage": job["stage"],
        "progress": job.get("progress", 0.0), "error": job.get("error", ""), "info": job.get("info") or {},
        "language": job.get("language", ""), "model": job.get("model", ""), "style": job["style"],
        "notes": job.get("notes", []), "created": job.get("created", 0),
        "preview": f"/api/jobs/{job_id}/media" if job.get("preview") else "", "task": task,
        "options": {"language": options.get("language"), "model": options.get("model"),
                    "polish": bool(options.get("polish")), "canvas": options.get("canvas", "vertical")},
    }


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(name).stem).strip(" ._") or "captions"
    return stem[:80]


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    manager = JobManager(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        manager.shutdown()

    app = FastAPI(title="Hinglish Reel Captioner", version=__version__, docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.jobs = manager
    app.state.settings = settings
    ollama_cache: Dict[str, Any] = {"at": 0.0, "value": None}

    @app.exception_handler(NotFound)
    async def _not_found(_request: Request, _exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": "That file was not found. It may have been deleted."}, status_code=404)

    @app.exception_handler(JobError)
    async def _job_error(_request: Request, exc: JobError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.middleware("http")
    async def _cache_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/fonts/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path == "/" or path.startswith("/static/") or path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    def ollama_status() -> Dict[str, Any]:
        if time.time() - ollama_cache["at"] > 20 or ollama_cache["value"] is None:
            models = polish.list_models(settings.ollama_url)
            ollama_cache["value"] = {"available": bool(models), "models": models,
                                     "picked": polish.pick_model(models, settings.ollama_model)}
            ollama_cache["at"] = time.time()
        return ollama_cache["value"]

    @app.get("/api/status")
    async def status() -> Dict[str, Any]:
        demo = settings.asr_backend == "mock"
        return {
            "version": __version__, "demo": demo, "backend": settings.asr_backend,
            "device": "demo" if demo else resolve_device(settings.device),
            "default_model": settings.model,
            "models": [{**m, "cached": True if demo else is_model_cached(m["id"])} for m in WHISPER_MODELS],
            "languages": LANGUAGES, "presets": preset_catalog(), "tools": tools_status(),
            "ollama": ollama_status(), "max_upload_mb": settings.max_upload_mb,
        }

    @app.post("/api/jobs")
    async def create_job(
        file: UploadFile = File(...), language: str = Form("hinglish"), model: str = Form(""),
        hints: str = Form(""), user_words: str = Form(""), polish_cards: str = Form("0", alias="polish"),
        ollama_model: str = Form(""), canvas: str = Form("vertical"), style: str = Form(""),
    ) -> Dict[str, str]:
        if language not in LANGUAGE_IDS:
            raise HTTPException(400, "Unknown language option.")
        model = model.strip()[:200]
        if any(ord(ch) < 32 for ch in model):
            raise HTTPException(400, "That model name is not valid.")
        try:
            style_in = json.loads(style) if style.strip() else None
        except ValueError:
            style_in = None
        options = {
            "language": language, "model": model, "hints": hints.strip()[:400],
            "user_words": user_words[:20000], "polish": polish_cards.lower() in ("1", "true", "on", "yes"),
            "ollama_model": ollama_model.strip()[:100],
            "canvas": canvas if canvas in ("vertical", "square", "landscape") else "vertical",
            "style": style_in if isinstance(style_in, dict) else None,
        }
        job_id = manager.create(file.filename or "upload", options)
        dest = manager.source_path(job_id)
        limit = settings.max_upload_mb * 1024 * 1024
        size = 0
        try:
            with dest.open("wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"This file is larger than the {settings.max_upload_mb} MB limit.")
                    out.write(chunk)
        except HTTPException:
            manager.delete(job_id)
            raise
        except Exception:
            manager.delete(job_id)
            raise
        if size == 0:
            manager.delete(job_id)
            raise HTTPException(400, "That file is empty.")
        manager.start(job_id)
        return {"id": job_id}

    @app.get("/api/jobs")
    async def list_jobs() -> Any:
        return manager.list()

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> Dict[str, Any]:
        return _public(manager, manager.get(job_id))

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str) -> Dict[str, bool]:
        manager.get(job_id)
        manager.delete(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> Dict[str, bool]:
        manager.cancel(job_id)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/cards")
    async def get_cards(job_id: str) -> Dict[str, Any]:
        if manager.get(job_id)["status"] != "ready":
            raise JobError("Captions are not ready yet.")
        return manager.payload(job_id)

    async def _body(request: Request) -> Dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected a JSON body.") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "Expected a JSON object.")
        return data

    @app.post("/api/jobs/{job_id}/sync")
    async def sync(job_id: str, request: Request) -> Dict[str, Any]:
        body = await _body(request)
        style = body.get("style")
        cards = body.get("cards")
        if (style is not None and not isinstance(style, dict)) or (cards is not None and not isinstance(cards, list)):
            raise HTTPException(400, "style must be an object and cards a list.")
        return manager.sync(job_id, style, cards)

    @app.post("/api/jobs/{job_id}/regroup")
    async def regroup(job_id: str, request: Request) -> Dict[str, Any]:
        body = await _body(request)
        style = body.get("style")
        if style is not None and not isinstance(style, dict):
            raise HTTPException(400, "style must be an object.")
        return manager.regroup(job_id, style)

    @app.post("/api/jobs/{job_id}/render")
    async def render(job_id: str, request: Request) -> Dict[str, Any]:
        body = await _body(request)
        try:
            max_height = int(body.get("max_height") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "max_height must be a number.") from None
        if max_height and not 240 <= max_height <= 4320:
            raise HTTPException(400, "max_height must be between 240 and 4320.")
        manager.start_render(job_id, max_height)
        return _public(manager, manager.get(job_id))

    @app.get("/api/jobs/{job_id}/media")
    async def media(job_id: str) -> FileResponse:
        job = manager.get(job_id)
        if not job.get("preview"):
            raise HTTPException(404, "No preview yet.")
        path = manager.job_dir(job_id) / job["preview"]
        if not path.exists():
            raise HTTPException(404, "No preview yet.")
        return FileResponse(path)

    @app.get("/api/jobs/{job_id}/download/{kind}")
    async def download(job_id: str, kind: str) -> Response:
        if kind not in DOWNLOADS:
            raise HTTPException(404, "Unknown download.")
        job = manager.get(job_id)
        mime, ext = DOWNLOADS[kind]
        filename = f"{_safe_stem(job['name'])}{'-captioned' if kind == 'mp4' else ''}{ext}"
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        if kind == "mp4":
            name = (job.get("task") or {}).get("file")
            path = manager.job_dir(job_id) / name if name else None
            if not path or not path.exists():
                raise HTTPException(404, "Render the video first.")
            return FileResponse(path, media_type=mime, headers=headers)
        if job["status"] != "ready":
            raise JobError("Captions are not ready yet.")
        style = manager.style_of(job_id)
        cards = manager.load_cards(job_id)
        width, height = manager.canvas_dims(job_id)
        if kind == "srt":
            text = to_srt(cards, style, width, height)
        elif kind == "vtt":
            text = to_vtt(cards, style, width, height)
        elif kind == "ass":
            text = to_ass(cards, style, width, height)
        else:
            text = to_txt(cards)
        return Response(text.encode("utf-8"), media_type=mime, headers=headers)

    return app
