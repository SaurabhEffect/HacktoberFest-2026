"""Shared fixtures: tiny generated media files, settings that never touch the network, a fake Ollama."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List

import pytest
from fastapi.testclient import TestClient

from reelcaptioner.app import create_app
from reelcaptioner.config import Settings

from .helpers import HAVE_FFMPEG, ffmpeg


# --------------------------------------------------------------------------- media
@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def clip(media_dir: Path) -> Path:
    """6 s, 270x480 (9:16) test pattern with a tone: the stand-in for somebody's reel."""
    out = media_dir / "reel.mp4"
    if HAVE_FFMPEG:
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=270x480:rate=15:duration=6",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=6",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out)
    return out


@pytest.fixture(scope="session")
def flat_clip(media_dir: Path) -> Path:
    """Flat grey-green 360x640 clip, so the only thing that differs from the background is the caption."""
    out = media_dir / "flat.mp4"
    if HAVE_FFMPEG:
        ffmpeg("-f", "lavfi", "-i", "color=c=0x6b7f79:s=360x640:r=10:d=3",
               "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "3",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", out)
    return out


@pytest.fixture(scope="session")
def voice_note(media_dir: Path) -> Path:
    out = media_dir / "voice.wav"
    if HAVE_FFMPEG:
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=300:sample_rate=16000:duration=4", out)
    return out


@pytest.fixture(scope="session")
def silent_video(media_dir: Path) -> Path:
    """A video with no audio track at all."""
    out = media_dir / "silent.mp4"
    if HAVE_FFMPEG:
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=2", "-an",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out)
    return out


# --------------------------------------------------------------------------- app
def make_settings(root: Path, **overrides) -> Settings:
    base = dict(
        data_dir=root / "data", asr_backend="mock", model="small", device="cpu", compute_type="int8",
        cpu_threads=0, max_upload_mb=50,
        ollama_url="http://127.0.0.1:9",      # nothing listens on the discard port: "Ollama is not running"
        ollama_model="", host="127.0.0.1", port=8000,
    )
    base.update(overrides)
    base["data_dir"].mkdir(parents=True, exist_ok=True)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as c:
        yield c


def upload(client: TestClient, path: Path, **fields) -> str:
    with path.open("rb") as fh:
        resp = client.post("/api/jobs", files={"file": (path.name, fh, "application/octet-stream")}, data=fields)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def wait_for(client: TestClient, job_id: str, timeout: float = 90.0, until=("ready", "error", "cancelled")) -> dict:
    deadline = time.time() + timeout
    job: dict = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in until:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s: {job}")


def wait_task(client: TestClient, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    job: dict = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["task"]["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"render did not finish in {timeout}s: {job['task']}")


@pytest.fixture
def ready_job(client: TestClient, clip: Path) -> str:
    job_id = upload(client, clip, language="hinglish")
    job = wait_for(client, job_id)
    assert job["status"] == "ready", job
    return job_id


# --------------------------------------------------------------------------- fake Ollama
class FakeOllama:
    """Just enough of Ollama's HTTP API for the polish step. ``fixes`` maps wrong -> right spellings."""

    def __init__(self) -> None:
        self.models: List[str] = ["llama3.2:3b", "gemma3:4b"]
        self.fixes: Dict[str, str] = {}
        self.mode = "good"                  # good | wrong_count | garbage | invalid_json | dict | http_error
        self.requests: List[dict] = []
        self.url = ""
        self._server: ThreadingHTTPServer | None = None

    def reply(self, lines: List[str]):
        if self.mode == "wrong_count":
            return [line + " extra" for line in lines]
        if self.mode == "garbage":
            return ["zzzz qqqq xxxx " * 1 for _ in lines]
        out = [" ".join(self.fixes.get(tok, tok) for tok in line.split()) for line in lines]
        return {"lines": out} if self.mode == "dict" else out

    def start(self) -> "FakeOllama":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):          # keep pytest output clean
                pass

            def _send(self, code: int, payload) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):                    # noqa: N802
                if self.path == "/api/tags":
                    self._send(200, {"models": [{"name": n} for n in outer.models]})
                else:
                    self._send(404, {})

            def do_POST(self):                   # noqa: N802
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
                outer.requests.append(body)
                if outer.mode == "http_error":
                    self._send(500, {"error": "boom"})
                    return
                lines = json.loads(body["messages"][-1]["content"])
                if outer.mode == "invalid_json":
                    content = "this is not json"
                else:
                    content = json.dumps(outer.reply(lines), ensure_ascii=False)
                self._send(200, {"message": {"role": "assistant", "content": content}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=lambda: self._server.serve_forever(poll_interval=0.02), daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def fake_ollama():
    server = FakeOllama().start()
    yield server
    server.stop()


# --------------------------------------------------------------------------- helpers for tests
@pytest.fixture
def words() -> Callable:
    from reelcaptioner.captions import Word

    def make(text: str, start: float = 0.5, step: float = 0.4, dur: float = 0.3):
        return [Word(w, start + i * step, start + i * step + dur) for i, w in enumerate(text.split())]

    return make
