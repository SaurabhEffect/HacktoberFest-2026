"""Burn captions into the video with ffmpeg + libass (everything stays on this machine)."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from .captions import Card, Style
from .config import FONTS_DIR
from .exporters import output_size, to_ass
from .media import MediaError, run_ffmpeg, tools_status

CANVASES = {"vertical": (1080, 1920), "square": (1080, 1080), "landscape": (1920, 1080)}
AUDIO_BACKGROUND = "0x17302B"


def canvas_size(info: Dict[str, object], canvas: str = "vertical") -> tuple:
    if info.get("has_video") and info.get("width") and info.get("height"):
        return int(info["width"]), int(info["height"])
    return CANVASES.get(canvas, CANVASES["vertical"])


def render_video(
    job_dir: Path,
    source: Path,
    info: Dict[str, object],
    cards: Sequence[Card],
    style: Style,
    *,
    max_height: int = 0,
    canvas: str = "vertical",
    output_name: str = "captioned.mp4",
    on_progress: Optional[Callable[[float], None]] = None,
    cancel=None,
) -> Path:
    if not tools_status().get("libass"):
        raise MediaError("This FFmpeg build cannot draw subtitles (libass is missing). "
                         "Install a full FFmpeg build, or export the .srt/.ass file instead.")
    src_w, src_h = canvas_size(info, canvas)
    width, height = output_size(src_w, src_h, max_height)

    fonts = job_dir / "fonts"
    fonts.mkdir(exist_ok=True)
    for font in FONTS_DIR.glob("*.ttf"):
        target = fonts / font.name
        if not target.exists():
            shutil.copyfile(font, target)
    (job_dir / "captions.ass").write_text(to_ass(cards, style, width, height), encoding="utf-8")

    chain = ["ass=captions.ass:fontsdir=fonts"]
    if info.get("has_video"):
        if (width, height) != (int(info["width"]), int(info["height"])):
            chain.insert(0, f"scale={width}:{height}:flags=lanczos")
        args = ["-i", source, "-map", "0:v:0", "-map", "0:a:0?"]
    else:
        args = ["-f", "lavfi", "-i", f"color=c={AUDIO_BACKGROUND}:s={width}x{height}:r=30",
                "-i", source, "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    out = job_dir / output_name
    args += ["-vf", ",".join(chain), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name]
    run_ffmpeg(args, duration=float(info["duration"]), on_progress=on_progress, cancel=cancel, cwd=job_dir)
    if not out.exists() or out.stat().st_size == 0:
        raise MediaError("The render finished but no video was written.")
    return out
