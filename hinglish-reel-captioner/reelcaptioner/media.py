"""Thin, defensive wrappers around ffmpeg / ffprobe."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Optional


class MediaError(RuntimeError):
    """A user-presentable problem with a media file or with ffmpeg itself."""


class Cancelled(Exception):
    """Raised inside a worker when the user cancels a job."""


def _find(name: str, env_var: str) -> str:
    explicit = os.environ.get(env_var)
    if explicit:
        return explicit
    found = shutil.which(name)
    if not found:
        raise MediaError(
            f"'{name}' was not found. Install FFmpeg (https://ffmpeg.org/download.html) "
            f"and make sure '{name}' is on your PATH."
        )
    return found


def ffmpeg_bin() -> str:
    return _find("ffmpeg", "HRC_FFMPEG")


def ffprobe_bin() -> str:
    return _find("ffprobe", "HRC_FFPROBE")


@lru_cache(maxsize=1)
def tools_status() -> Dict[str, object]:
    """Report whether ffmpeg is usable and can burn subtitles (libass)."""
    status: Dict[str, object] = {"ffmpeg": False, "ffprobe": False, "libass": False, "version": ""}
    try:
        exe = ffmpeg_bin()
        ffprobe_bin()
        status["ffmpeg"] = status["ffprobe"] = True
        out = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=20).stdout
        status["version"] = (out.splitlines() or [""])[0][:80]
        filters = subprocess.run([exe, "-hide_banner", "-filters"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=20).stdout
        status["libass"] = any(f" {name} " in filters for name in ("ass", "subtitles"))
    except (MediaError, OSError, subprocess.SubprocessError):
        pass
    return status


def _fps(value: Optional[str]) -> float:
    try:
        if not value or value == "0/0":
            return 0.0
        if "/" in value:
            num, den = value.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: Path) -> Dict[str, object]:
    """Return duration, display size and stream info for a media file."""
    cmd = [ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Reading the file took too long. Is it a valid video or audio file?") from exc
    if run.returncode != 0:
        raise MediaError("This does not look like a video or audio file (" + run.stderr.strip()[-160:] + ")")
    try:
        data = json.loads(run.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError("Could not read the media information from this file.") from exc

    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    def is_cover_art(stream: dict) -> bool:
        return bool((stream.get("disposition") or {}).get("attached_pic"))

    video = next((s for s in streams if s.get("codec_type") == "video" and not is_cover_art(s)), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise MediaError("This file has no audio track, so there is nothing to caption.")

    duration = 0.0
    for candidate in (fmt.get("duration"), (video or {}).get("duration"), audio.get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue
    if duration <= 0:
        raise MediaError("Could not work out how long this file is.")

    width = height = 0
    fps = 0.0
    if video is not None:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        fps = _fps(video.get("avg_frame_rate")) or _fps(video.get("r_frame_rate"))
        rotation = 0
        try:
            rotation = int(float((video.get("tags") or {}).get("rotate", 0)))
        except (TypeError, ValueError):
            rotation = 0
        for side in video.get("side_data_list") or []:
            if "rotation" in side:
                try:
                    rotation = int(float(side["rotation"]))
                except (TypeError, ValueError):
                    pass
        if abs(rotation) % 180 == 90:
            width, height = height, width
        if width <= 0 or height <= 0:
            video = None

    return {
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "has_video": video is not None,
        "has_audio": True,
        "video_codec": (video or {}).get("codec_name", ""),
        "audio_codec": audio.get("codec_name", ""),
        "size_bytes": int(fmt.get("size") or 0) or (Path(path).stat().st_size if Path(path).exists() else 0),
    }


def run_ffmpeg(
    args,
    *,
    duration: Optional[float] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    cancel: Optional[threading.Event] = None,
    cwd: Optional[Path] = None,
) -> None:
    """Run ffmpeg, reporting 0..1 progress from its -progress stream."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    cmd += [str(a) for a in args]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd) if cwd else None, bufsize=1,
    )
    tail: deque = deque(maxlen=40)

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line.rstrip())

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise Cancelled()
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    micros = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if duration and on_progress and micros >= 0:
                    on_progress(min(1.0, micros / 1_000_000.0 / duration))
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
        drainer.join(timeout=3)
    if proc.returncode != 0:
        detail = " ".join(list(tail)[-4:]).strip() or f"exit code {proc.returncode}"
        raise MediaError("ffmpeg could not process this file: " + detail[-400:])


def extract_audio(src: Path, dst: Path, *, duration: float,
                  on_progress=None, cancel=None) -> None:
    """16 kHz mono WAV, the format Whisper wants."""
    run_ffmpeg(["-i", src, "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dst],
               duration=duration, on_progress=on_progress, cancel=cancel)


def make_preview(src: Path, dst: Path, info: Dict[str, object], *, on_progress=None, cancel=None) -> None:
    """A small, browser-friendly copy (H.264 + AAC, or AAC only) for the in-app player."""
    duration = float(info["duration"])
    if info["has_video"]:
        scale = "scale='if(gt(iw,ih),min(960,iw),-2)':'if(gt(iw,ih),-2,min(960,ih))'"
        run_ffmpeg(
            ["-i", src, "-map", "0:v:0", "-map", "0:a:0", "-vf", scale + ",format=yuv420p",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", dst],
            duration=duration, on_progress=on_progress, cancel=cancel)
    else:
        run_ffmpeg(["-i", src, "-vn", "-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst],
                   duration=duration, on_progress=on_progress, cancel=cancel)
