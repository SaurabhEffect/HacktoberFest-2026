"""Small shared helpers for the test modules (kept out of conftest.py, which pytest manages itself)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="FFmpeg is not installed")


def _has_libass() -> bool:
    if not HAVE_FFMPEG:
        return False
    from reelcaptioner.media import tools_status
    return bool(tools_status().get("libass"))


needs_libass = pytest.mark.skipif(not _has_libass(), reason="this FFmpeg build has no libass")


def ffmpeg(*args) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *map(str, args)], check=True)
