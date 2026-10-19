"""Job store and background worker.

Every upload becomes a job: a folder under ``data/jobs/<id>`` holding the source file, a small
preview copy, the transcript, the (editable) caption cards and any rendered output. Jobs are
persisted as JSON so a page refresh, or even a server restart, never loses someone's work.
One worker thread runs the heavy steps one at a time, which keeps a laptop responsive.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import media, polish
from .asr import ModelError, get_transcriber
from .captions import (Card, Style, cards_from_dicts, flatten_words, group_words, layout_cards,
                       normalize_cards)
from .config import Settings
from .exporters import position_spec
from .media import Cancelled, MediaError
from .pipeline import build_words
from .render import render_video
from .romanize import parse_user_words

log = logging.getLogger("reelcaptioner")
JOB_ID = re.compile(r"^[a-f0-9]{16,32}$")
ACTIVE = {"queued", "working"}


class JobError(RuntimeError):
    """A problem with the user's content that should be shown as-is."""


class NotFound(KeyError):
    pass


def _atomic_write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class JobManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._cancel: Dict[str, threading.Event] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reelcaptioner")
        self._load_existing()

    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID.match(job_id or ""):
            raise NotFound(job_id)
        return self.root / job_id

    def _load_existing(self) -> None:
        for folder in self.root.iterdir():
            meta = folder / "job.json"
            if not (folder.is_dir() and meta.exists()):
                continue
            try:
                job = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("status") in ACTIVE:
                job["status"], job["stage"] = "error", "Interrupted"
                job["error"] = "The app was closed while this was running. Upload the file again to retry."
            task = job.get("task") or {}
            if task.get("status") == "working":
                job["task"] = {**task, "status": "error", "message": "Interrupted by a restart."}
            self._jobs[job["id"]] = job

    def _save(self, job: Dict[str, Any]) -> None:
        with self._lock:
            try:
                _atomic_write(self.job_dir(job["id"]) / "job.json", job)
            except FileNotFoundError:      # the job was deleted while a worker was still running
                pass

    def _update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:                # deleted while running: nothing left to update
                return
            job.update(fields)
            self._save(job)

    def _progress(self, job_id: str, value: float) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["progress"] = round(max(0.0, min(1.0, value)), 4)   # transient: no disk write

    def get(self, job_id: str) -> Dict[str, Any]:
        self.job_dir(job_id)
        with self._lock:
            if job_id not in self._jobs:
                raise NotFound(job_id)
            return json.loads(json.dumps(self._jobs[job_id]))

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.get("created", 0), reverse=True)
            return [{k: j.get(k) for k in ("id", "name", "status", "stage", "created", "info")} for j in jobs]

    def create(self, filename: str, options: Dict[str, Any]) -> str:
        """Reserve a job folder. The caller streams the upload into ``source_path`` then ``start``."""
        job_id = uuid.uuid4().hex[:20]
        folder = self.root / job_id
        folder.mkdir(parents=True)
        suffix = Path(filename).suffix.lower()
        if not re.match(r"^\.[a-z0-9]{1,5}$", suffix):
            suffix = ".media"
        job = {
            "id": job_id, "name": Path(filename).name[:120] or "upload", "created": time.time(),
            "status": "queued", "stage": "Uploading", "progress": 0.0, "error": "",
            "source": f"source{suffix}", "options": options, "info": {}, "language": "", "model": "",
            "style": Style.from_dict(options.get("style")).to_dict(), "preview": "",
            "task": {"kind": "", "status": "idle", "progress": 0.0, "message": "", "file": ""},
            "notes": [],
        }
        with self._lock:
            self._jobs[job_id] = job
            self._save(job)
        return job_id

    def source_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / self.get(job_id)["source"]

    def start(self, job_id: str) -> None:
        self._cancel[job_id] = threading.Event()
        self._update(job_id, stage="Waiting in line")
        self._pool.submit(self._run_pipeline, job_id)

    def cancel(self, job_id: str) -> None:
        self.get(job_id)
        event = self._cancel.get(job_id)
        if event is not None:
            event.set()

    def delete(self, job_id: str) -> None:
        self.cancel(job_id)
        folder = self.job_dir(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        shutil.rmtree(folder, ignore_errors=True)

    def _cards_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "cards.json"

    def load_cards(self, job_id: str) -> List[Card]:
        path = self._cards_path(job_id)
        if not path.exists():
            return []
        return cards_from_dicts(json.loads(path.read_text(encoding="utf-8")))

    def save_cards(self, job_id: str, cards: List[Card]) -> None:
        with self._lock:
            _atomic_write(self._cards_path(job_id), [c.to_dict() for c in cards])

    def style_of(self, job_id: str) -> Style:
        return Style.from_dict(self.get(job_id).get("style"))

    def aspect_of(self, job_id: str) -> float:
        info = self.get(job_id).get("info") or {}
        w, h = int(info.get("width") or 0), int(info.get("height") or 0)
        if w and h:
            return w / float(h)
        canvas = (self.get(job_id).get("options") or {}).get("canvas", "vertical")
        return {"vertical": 9 / 16.0, "square": 1.0, "landscape": 16 / 9.0}.get(canvas, 9 / 16.0)

    def canvas_dims(self, job_id: str) -> tuple:
        from .render import canvas_size
        job = self.get(job_id)
        return canvas_size(job.get("info") or {}, (job.get("options") or {}).get("canvas", "vertical"))

    def sync(self, job_id: str, style_in: Optional[dict], cards_in: Optional[list]) -> Dict[str, Any]:
        """Apply a style and/or edited cards, then return laid-out cards for the editor."""
        job = self.get(job_id)
        if job["status"] != "ready":
            raise JobError("Captions are not ready yet.")
        # The editor always sends a complete style; anything missing falls back to its preset.
        style = Style.from_dict(style_in if style_in is not None else job["style"])
        duration = float((job.get("info") or {}).get("duration") or 0) or 3600.0
        cards = normalize_cards(cards_in, duration) if cards_in is not None else self.load_cards(job_id)
        layout_cards(cards, style, self.aspect_of(job_id))
        if cards_in is not None:
            self.save_cards(job_id, cards)
        if style_in is not None:
            self._update(job_id, style=style.to_dict())
        return self._payload(job_id, cards, style)

    def regroup(self, job_id: str, style_in: Optional[dict]) -> Dict[str, Any]:
        job = self.get(job_id)
        if job["status"] != "ready":
            raise JobError("Captions are not ready yet.")
        style = Style.from_dict(style_in if style_in is not None else job["style"])
        words = flatten_words(self.load_cards(job_id))
        duration = float((job.get("info") or {}).get("duration") or 0) or None
        cards = group_words(words, style, self.aspect_of(job_id), duration)
        self.save_cards(job_id, cards)
        self._update(job_id, style=style.to_dict())
        return self._payload(job_id, cards, style)

    def payload(self, job_id: str) -> Dict[str, Any]:
        style = self.style_of(job_id)
        cards = self.load_cards(job_id)
        layout_cards(cards, style, self.aspect_of(job_id))
        return self._payload(job_id, cards, style)

    def _payload(self, job_id: str, cards: List[Card], style: Style) -> Dict[str, Any]:
        from .captions import display_text
        width, height = self.canvas_dims(job_id)
        out = []
        for card in cards:
            item = card.to_dict()
            item["d"] = [display_text(w.w, style) for w in card.words]
            out.append(item)
        return {"cards": out, "style": style.to_dict(), "position": position_spec(style, width, height)}

    def _run_pipeline(self, job_id: str) -> None:
        cancel = self._cancel[job_id]
        folder = self.job_dir(job_id)
        try:
            job = self.get(job_id)
            opts = job["options"]
            source = folder / job["source"]
            self._update(job_id, status="working", stage="Reading your file", progress=0.01)
            info = media.probe(source)
            self._update(job_id, info=info)

            self._update(job_id, stage="Extracting the audio")
            audio = folder / "audio.wav"
            media.extract_audio(source, audio, duration=info["duration"], cancel=cancel,
                                on_progress=lambda p: self._progress(job_id, 0.02 + 0.06 * p))

            self._update(job_id, stage="Making a preview")
            preview = folder / ("preview.mp4" if info["has_video"] else "preview.m4a")
            media.make_preview(source, preview, info, cancel=cancel,
                               on_progress=lambda p: self._progress(job_id, 0.08 + 0.12 * p))
            self._update(job_id, preview=preview.name)

            transcriber = get_transcriber(self.settings)
            model = (opts.get("model") or self.settings.model).strip() or self.settings.model
            transcript = transcriber.transcribe(
                audio, language_mode=opts.get("language", "hinglish"), model=model,
                hints=opts.get("hints", ""), duration=info["duration"], cancel=cancel,
                on_progress=lambda p: self._progress(job_id, 0.20 + 0.68 * p),
                status=lambda s: self._update(job_id, stage=s))
            if not transcript.words:
                raise JobError("No speech was found in this file. Check that the audio is audible, "
                               "or try a larger model.")
            _atomic_write(folder / "transcript.json", {
                "language": transcript.language, "model": transcript.model, "duration": transcript.duration,
                "words": [{"t": w.text, "s": w.start, "e": w.end, "p": w.prob} for w in transcript.words]})

            self._update(job_id, stage="Writing Roman Hinglish", language=transcript.language,
                         model=transcript.model)
            words = build_words(transcript.words, parse_user_words(opts.get("user_words", "")))
            style = Style.from_dict(job["style"])
            cards = group_words(words, style, self.aspect_of(job_id), info["duration"])
            if not cards:
                raise JobError("The recording was transcribed but produced no readable words.")

            notes: List[str] = []
            if opts.get("polish"):
                cards, notes = self._polish(job_id, cards, opts, cancel)
            self.save_cards(job_id, cards)
            self._update(job_id, status="ready", stage="Ready", progress=1.0, notes=notes)
        except Cancelled:
            self._update(job_id, status="cancelled", stage="Cancelled", error="")
        except (MediaError, ModelError, JobError) as exc:
            self._update(job_id, status="error", stage="Stopped", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - last resort so the UI never hangs
            log.error("pipeline failed for %s\n%s", job_id, traceback.format_exc())
            self._update(job_id, status="error", stage="Stopped",
                         error=f"Something unexpected went wrong ({type(exc).__name__}: {exc}).")

    def _polish(self, job_id: str, cards: List[Card], opts: Dict[str, Any], cancel) -> tuple:
        installed = polish.list_models(self.settings.ollama_url)
        model = polish.pick_model(installed, opts.get("ollama_model") or self.settings.ollama_model)
        if not model:
            return cards, ["Polish skipped: Ollama is not running or has no models installed."]
        self._update(job_id, stage=f"Polishing spelling with {model}")
        stats = polish.polish_cards(cards, self.settings.ollama_url, model, cancel=cancel,
                                    on_progress=lambda p: self._progress(job_id, 0.88 + 0.10 * p))
        note = f"Polished with {model}: {stats['changed']} cards adjusted, {stats['kept']} left as they were."
        if stats["failed"]:
            note += f" {stats['failed']} batches could not be processed."
        return cards, [note]

    def _set_task(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            task = dict(job.get("task") or {})
            task.update(fields)
            job["task"] = task
            if any(key in fields for key in ("status", "kind", "file")):   # skip per-tick progress
                self._save(job)

    def start_render(self, job_id: str, max_height: int = 0) -> None:
        job = self.get(job_id)
        if job["status"] != "ready":
            raise JobError("Captions are not ready yet.")
        if (job.get("task") or {}).get("status") == "working":
            raise JobError("Another step is still running for this file.")
        self._cancel[job_id] = threading.Event()
        self._set_task(job_id, kind="render", status="working", progress=0.0,
                       message="Waiting in line", file="")
        self._pool.submit(self._run_render, job_id, int(max_height or 0))

    def _run_render(self, job_id: str, max_height: int) -> None:
        cancel = self._cancel[job_id]
        try:
            job = self.get(job_id)
            folder = self.job_dir(job_id)
            style = Style.from_dict(job["style"])
            cards = self.load_cards(job_id)
            self._set_task(job_id, message="Burning captions into your video")
            out = render_video(
                folder, folder / job["source"], job["info"], cards, style, max_height=max_height,
                canvas=(job.get("options") or {}).get("canvas", "vertical"), cancel=cancel,
                on_progress=lambda p: self._set_task(job_id, progress=round(p, 4)))
            self._set_task(job_id, status="done", progress=1.0, message="Your video is ready", file=out.name)
        except Cancelled:
            self._set_task(job_id, status="error", message="Cancelled")
        except (MediaError, JobError) as exc:
            self._set_task(job_id, status="error", message=str(exc))
        except Exception as exc:  # noqa: BLE001
            log.error("render failed for %s\n%s", job_id, traceback.format_exc())
            self._set_task(job_id, status="error", message=f"Render failed ({type(exc).__name__}: {exc}).")

    def shutdown(self) -> None:
        for event in self._cancel.values():
            event.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
