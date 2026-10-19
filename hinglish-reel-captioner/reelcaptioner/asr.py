"""Speech recognition backends.

* ``FasterWhisperTranscriber``: open-weight Whisper models run locally through CTranslate2.
  Hindi/Hinglish is decoded as Hindi (Devanagari with English words left in Latin), then the
  romanizer turns it into the Roman script creators actually post.
* ``MockTranscriber``: a deterministic demo backend so the whole app can be tried (and tested)
  without downloading a model.
"""
from __future__ import annotations

import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np

from .config import Settings
from .media import Cancelled

HINGLISH_PROMPT = "ये एक Hinglish video है, जिसमें हम Hindi और English mix करके बात करते हैं।"
HINDI_LIKE = {"hi", "ur"}


@dataclass
class RawWord:
    text: str
    start: float
    end: float
    prob: float = 1.0


@dataclass
class Transcript:
    language: str
    duration: float
    model: str
    words: List[RawWord] = field(default_factory=list)


ProgressFn = Callable[[float], None]
StatusFn = Callable[[str], None]


def _noop(*_args, **_kwargs) -> None:
    return None


class ModelError(RuntimeError):
    """The speech model could not be loaded or run (message is shown to the user)."""


_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: Dict[tuple, object] = {}


def resolve_device(device: str) -> str:
    if device in ("cpu", "cuda"):
        return device
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:  # noqa: BLE001 - any import/driver problem simply means "use the CPU"
        return "cpu"


def resolve_compute_type(compute_type: str, device: str) -> str:
    if compute_type != "auto":
        return compute_type
    return "float16" if device == "cuda" else "int8"


def is_model_cached(model: str) -> bool:
    """True when the model can be loaded without touching the network."""
    if Path(model).expanduser().is_dir():
        return True
    try:
        from faster_whisper.utils import download_model
        download_model(model, local_files_only=True)
        return True
    except Exception:  # noqa: BLE001 - not cached (or hub client unavailable)
        return False


def load_whisper(model: str, settings: Settings, status: StatusFn = _noop):
    device = resolve_device(settings.device)
    compute = resolve_compute_type(settings.compute_type, device)
    key = (model, device, compute, settings.cpu_threads)
    with _MODEL_LOCK:
        if key in _MODEL_CACHE:
            return _MODEL_CACHE[key]
        from faster_whisper import WhisperModel
        cached = is_model_cached(model)
        status("Loading the speech model" if cached else
               "Downloading the open model, this happens once and then it works offline")
        try:
            instance = WhisperModel(str(Path(model).expanduser()) if Path(model).expanduser().is_dir() else model,
                                    device=device, compute_type=compute, cpu_threads=settings.cpu_threads)
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            if any(marker in detail.lower() for marker in ("mkl_malloc", "failed to allocate memory", "out of memory")):
                raise ModelError(
                    f"Could not load the speech model '{model}': {detail}. "
                    "The model could not allocate enough memory. Try --model tiny or --model base, close other "
                    "memory-heavy apps, or set HRC_CPU_THREADS=2 before starting the app. On Windows, make sure "
                    "virtual memory (the paging file) is enabled."
                ) from exc
            hint = ("" if cached else " The first run needs internet to fetch the model; after that it is "
                    "fully offline. You can also point HRC_MODEL at a local CTranslate2 model folder.")
            raise ModelError(f"Could not load the speech model '{model}': {detail}.{hint}") from exc
        _MODEL_CACHE[key] = instance
        return instance


def read_wav_head(path: Path, seconds: float = 30.0) -> np.ndarray:
    """First ``seconds`` of a 16 kHz mono PCM WAV as float32 in [-1, 1]."""
    with wave.open(str(path), "rb") as wav:
        frames = wav.readframes(int(wav.getframerate() * seconds))
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def pick_language(all_probs, fallback: str) -> str:
    """Hindi, Urdu and English are what creators post: choose between them, else trust Whisper."""
    probs = {lang: float(p) for lang, p in all_probs}
    hindi = probs.get("hi", 0.0) + probs.get("ur", 0.0)
    english = probs.get("en", 0.0)
    if max(hindi, english) >= 0.35:
        return "hi" if hindi >= english else "en"
    return fallback


class FasterWhisperTranscriber:
    name = "faster-whisper"

    def __init__(self, settings: Settings):
        self.settings = settings

    def transcribe(self, audio: Path, *, language_mode: str, model: str, hints: str,
                   duration: float, on_progress: ProgressFn = _noop, status: StatusFn = _noop,
                   cancel: Optional[threading.Event] = None) -> Transcript:
        whisper = load_whisper(model, self.settings, status)
        status("Listening")
        language: Optional[str] = {"hinglish": "hi", "english": "en"}.get(language_mode)
        if language_mode == "auto":
            try:
                found, _prob, probs = whisper.detect_language(read_wav_head(audio), vad_filter=True)
                language = pick_language(probs, found)
            except Exception:  # noqa: BLE001 - detection is a nicety, transcription can still decide
                language = None
        prompt = HINGLISH_PROMPT if language == "hi" else ""
        if hints.strip():
            prompt = (prompt + " " + hints.strip()).strip()
        small = model in ("tiny", "base", "tiny.en", "base.en")
        try:
            segments, info = whisper.transcribe(
                str(audio), language=language, task="transcribe", beam_size=3 if small else 5,
                word_timestamps=True, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 350, "speech_pad_ms": 200},
                condition_on_previous_text=False, initial_prompt=prompt or None,
                hotwords=hints.strip() or None, hallucination_silence_threshold=2.0)
        except Exception as exc:  # noqa: BLE001
            raise ModelError(f"Transcription failed: {exc}") from exc

        words: List[RawWord] = []
        total = max(duration, info.duration or 0.0, 0.1)
        for segment in segments:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if segment.words:
                for w in segment.words:
                    text = (w.word or "").strip()
                    if text:
                        words.append(RawWord(text, float(w.start), float(w.end), float(w.probability or 1.0)))
            else:  # no word timings for this segment: spread its words evenly
                tokens = (segment.text or "").split()
                span = max(float(segment.end) - float(segment.start), 0.05 * max(len(tokens), 1))
                for i, token in enumerate(tokens):
                    a = float(segment.start) + span * i / len(tokens)
                    b = float(segment.start) + span * (i + 1) / len(tokens)
                    words.append(RawWord(token, a, b))
            on_progress(min(1.0, float(segment.end) / total))
        return Transcript(language=info.language or (language or ""), duration=total, model=model, words=words)


DEMO_SCRIPT = (
    "नमस्ते दोस्तों, आज हम बात करेंगे कि कैसे आप अपने reels में Hinglish captions लगा सकते हो। "
    "ये सब बिल्कुल free है, और आपका video आपके laptop से बाहर नहीं जाता। "
    "कोई API नहीं, कोई subscription नहीं। "
    "बस video drop करो, captions edit करो, और download कर लो। "
    "तो चलिए शुरू करते हैं, और अपनी रील को वायरल बनाते हैं!"
).split()


class MockTranscriber:
    """Plays a fixed Hinglish script over the clip. Lets people try the app with no model."""

    name = "mock"

    def __init__(self, settings: Settings, delay: float = 0.0):
        self.settings = settings
        self.delay = delay

    def transcribe(self, audio: Path, *, language_mode: str, model: str, hints: str,
                   duration: float, on_progress: ProgressFn = _noop, status: StatusFn = _noop,
                   cancel: Optional[threading.Event] = None) -> Transcript:
        status("Demo mode: no model needed")
        words: List[RawWord] = []
        t, i = min(0.4, duration / 10.0), 0
        while t < duration - 0.3:
            token = DEMO_SCRIPT[i % len(DEMO_SCRIPT)]
            length = 0.16 + 0.035 * len(token)
            end = min(t + length, duration - 0.05)
            words.append(RawWord(token, t, end, 0.95))
            t = end + 0.05 + (0.30 if token.endswith(("।", ",", "!", ".")) else 0.0)
            i += 1
        steps = 10
        for step in range(steps):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if self.delay:
                time.sleep(self.delay / steps)
            on_progress((step + 1) / steps)
        return Transcript(language="hi", duration=duration, model="demo", words=words)


def get_transcriber(settings: Settings):
    if settings.asr_backend == "mock":
        try:
            import os
            delay = float(os.environ.get("HRC_MOCK_DELAY", "0"))
        except ValueError:
            delay = 0.0
        return MockTranscriber(settings, delay=delay)
    return FasterWhisperTranscriber(settings)
