"""Paths, environment settings and the few constants shared across modules."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "static"
FONTS_DIR = STATIC_DIR / "fonts"

# Whisper models faster-whisper can fetch by name (CTranslate2 conversions).
# Any local folder or Hugging Face repo id also works via the "custom" field.
#   label: short enough to read in a closed drop-down, note: shown under it for the selected model.
WHISPER_MODELS = [
    {"id": "tiny", "label": "tiny · 75 MB · fastest", "note": "The fastest, but rough on Hinglish"},
    {"id": "base", "label": "base · 145 MB · quick", "note": "Quick drafts"},
    {"id": "small", "label": "small · 480 MB · default", "note": "A solid default on a laptop CPU"},
    {"id": "medium", "label": "medium · 1.5 GB · better", "note": "Noticeably better Hindi, slower on a CPU"},
    {"id": "large-v3-turbo", "label": "large-v3-turbo · 1.6 GB", "note": "The best accuracy per second"},
    {"id": "large-v3", "label": "large-v3 · 3 GB · best", "note": "The most accurate, slow on a CPU"},
]

LANGUAGES = [
    {"id": "hinglish", "label": "Hinglish (Hindi + English)"},
    {"id": "english", "label": "English"},
    {"id": "auto", "label": "Auto-detect"},
]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    asr_backend: str      # "faster-whisper" or "mock" (tests and demo)
    model: str            # default Whisper model
    device: str           # auto | cpu | cuda
    compute_type: str     # auto | int8 | float16 | ...
    cpu_threads: int      # 0 = let CTranslate2 decide
    max_upload_mb: int
    ollama_url: str
    ollama_model: str     # "" = pick automatically
    host: str
    port: int


class ConfigError(ValueError):
    """A setting cannot be used. The message is written for a person, not for a traceback."""


def _whole_number(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, but it is {raw!r}.") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, but it is {value}.")
    return value


def get_settings() -> Settings:
    """Read settings from the environment on every call so tests can override them."""
    raw_dir = os.environ.get("HRC_DATA_DIR") or str(PROJECT_DIR / "data")
    data_dir = Path(raw_dir).expanduser().resolve()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Cannot use {data_dir} as the data folder ({exc.strerror or exc}). "
                          "Pick another one with --data-dir.") from None
    return Settings(
        data_dir=data_dir,
        asr_backend=os.environ.get("HRC_ASR_BACKEND", "faster-whisper").strip().lower(),
        model=os.environ.get("HRC_MODEL", "small").strip() or "small",
        device=os.environ.get("HRC_DEVICE", "auto").strip().lower(),
        compute_type=os.environ.get("HRC_COMPUTE_TYPE", "auto").strip().lower(),
        cpu_threads=_whole_number("HRC_CPU_THREADS", 0, 0, 1024),
        max_upload_mb=_whole_number("HRC_MAX_UPLOAD_MB", 2048, 1, 1_000_000),
        ollama_url=os.environ.get("HRC_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
        ollama_model=os.environ.get("HRC_OLLAMA_MODEL", "").strip(),
        host=os.environ.get("HRC_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=_whole_number("HRC_PORT", 8000, 1, 65535),
    )
