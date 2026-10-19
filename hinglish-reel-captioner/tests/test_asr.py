"""Speech backends. The Whisper adapter is exercised against a fake model with faster-whisper's real shapes."""
import threading
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from reelcaptioner import asr
from reelcaptioner.media import Cancelled


# --------------------------------------------------------------------------- a stand-in for WhisperModel
def fw_word(text, start, end, prob=0.9):
    return SimpleNamespace(word=text, start=start, end=end, probability=prob)


def fw_segment(start, end, words=None, text=""):
    return SimpleNamespace(start=start, end=end, text=text, words=words)


class FakeWhisper:
    def __init__(self, segments, language="hi", duration=6.0, detected=("hi", 0.9, (("hi", 0.9), ("en", 0.05))),
                 fail=None):
        self.segments, self.language, self.duration = segments, language, duration
        self.detected, self.fail = detected, fail
        self.transcribe_kwargs = None
        self.detect_calls = 0

    def transcribe(self, audio, **kwargs):
        if self.fail:
            raise self.fail
        self.transcribe_kwargs = {"audio": audio, **kwargs}
        return iter(self.segments), SimpleNamespace(language=self.language, duration=self.duration)

    def detect_language(self, audio, vad_filter=False, **_):
        self.detect_calls += 1
        if isinstance(self.detected, Exception):
            raise self.detected
        return self.detected


@pytest.fixture
def silence(tmp_path):
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(np.zeros(16000 * 2, dtype=np.int16).tobytes())
    return path


@pytest.fixture
def transcriber(settings, monkeypatch):
    def install(fake):
        monkeypatch.setattr(asr, "load_whisper", lambda model, st, status=asr._noop: fake)
        return asr.FasterWhisperTranscriber(settings)
    return install


def run(t, audio, **kw):
    args = dict(language_mode="hinglish", model="small", hints="", duration=6.0)
    args.update(kw)
    return t.transcribe(audio, **args)


# --------------------------------------------------------------------------- language handling
@pytest.mark.parametrize("mode, expected", [("hinglish", "hi"), ("english", "en")])
def test_fixed_language_modes(transcriber, silence, mode, expected):
    fake = FakeWhisper([fw_segment(0, 1, [fw_word(" hello", 0, 1)])])
    run(transcriber(fake), silence, language_mode=mode)
    assert fake.transcribe_kwargs["language"] == expected
    assert fake.detect_calls == 0


def test_auto_mode_detects_between_hindi_and_english(transcriber, silence):
    fake = FakeWhisper([fw_segment(0, 1, [fw_word("hi", 0, 1)])],
                       detected=("ur", 0.5, (("ur", 0.5), ("hi", 0.1), ("en", 0.2))))
    run(transcriber(fake), silence, language_mode="auto")
    assert fake.detect_calls == 1 and fake.transcribe_kwargs["language"] == "hi"      # Urdu + Hindi beat English


def test_auto_mode_survives_a_failing_detector(transcriber, silence):
    fake = FakeWhisper([fw_segment(0, 1, [fw_word("hi", 0, 1)])], detected=RuntimeError("no detector"))
    run(transcriber(fake), silence, language_mode="auto")
    assert fake.transcribe_kwargs["language"] is None                                  # Whisper decides itself


@pytest.mark.parametrize("probs, fallback, expected", [
    ((("hi", 0.6), ("en", 0.2)), "en", "hi"),
    ((("ur", 0.4), ("hi", 0.1), ("en", 0.3)), "en", "hi"),
    ((("en", 0.7), ("hi", 0.1)), "hi", "en"),
    ((("fr", 0.9), ("en", 0.05), ("hi", 0.02)), "fr", "fr"),          # neither is plausible: trust Whisper
    ((("hi", 0.4), ("en", 0.4)), "en", "hi"),                          # a tie goes to Hinglish
])
def test_pick_language(probs, fallback, expected):
    assert asr.pick_language(probs, fallback) == expected


# --------------------------------------------------------------------------- what is sent to the model
def test_hinglish_prompt_hints_and_decoding_options(transcriber, silence):
    fake = FakeWhisper([fw_segment(0, 1, [fw_word("x", 0, 1)])])
    run(transcriber(fake), silence, hints="Gemma, Render", model="medium")
    kw = fake.transcribe_kwargs
    assert kw["audio"] == str(silence)
    assert kw["language"] == "hi" and kw["task"] == "transcribe"
    assert kw["initial_prompt"].startswith(asr.HINGLISH_PROMPT) and kw["initial_prompt"].endswith("Gemma, Render")
    assert kw["hotwords"] == "Gemma, Render"
    assert kw["word_timestamps"] is True and kw["vad_filter"] is True
    assert kw["beam_size"] == 5 and kw["condition_on_previous_text"] is False


def test_english_without_hints_sends_no_prompt_and_small_models_use_a_narrower_beam(transcriber, silence):
    fake = FakeWhisper([fw_segment(0, 1, [fw_word("x", 0, 1)])])
    run(transcriber(fake), silence, language_mode="english", model="tiny")
    assert fake.transcribe_kwargs["initial_prompt"] is None and fake.transcribe_kwargs["hotwords"] is None
    assert fake.transcribe_kwargs["beam_size"] == 3


# --------------------------------------------------------------------------- reading the result
def test_words_segments_and_progress_come_back_in_order(transcriber, silence):
    fake = FakeWhisper([
        fw_segment(0.0, 2.0, [fw_word(" नमस्ते", 0.1, 0.7, 0.99), fw_word(" ", 0.7, 0.8), fw_word("दोस्तों", 0.9, 1.6, None)]),
        fw_segment(2.0, 4.0, [fw_word("आज", 2.2, 2.8)]),
    ], language="hi", duration=4.0)
    seen = []
    out = run(transcriber(fake), silence, duration=4.0, on_progress=seen.append)
    assert [(w.text, w.start, w.end) for w in out.words] == [("नमस्ते", 0.1, 0.7), ("दोस्तों", 0.9, 1.6), ("आज", 2.2, 2.8)]
    assert out.words[0].prob == pytest.approx(0.99) and out.words[1].prob == 1.0        # a missing probability becomes 1
    assert out.language == "hi" and out.model == "small" and out.duration == 4.0
    assert seen == pytest.approx([0.5, 1.0])


def test_a_segment_without_word_timings_is_spread_evenly(transcriber, silence):
    fake = FakeWhisper([fw_segment(1.0, 4.0, None, text="ek do teen")])
    out = run(transcriber(fake), silence)
    assert [w.text for w in out.words] == ["ek", "do", "teen"]
    assert [w.start for w in out.words] == pytest.approx([1.0, 2.0, 3.0])
    assert out.words[-1].end == pytest.approx(4.0)


def test_cancelling_between_segments(transcriber, silence):
    fake = FakeWhisper([fw_segment(0, 1, [fw_word("a", 0, 1)]), fw_segment(1, 2, [fw_word("b", 1, 2)])])
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        run(transcriber(fake), silence, cancel=cancel)


def test_a_failing_model_becomes_a_friendly_error(transcriber, silence):
    fake = FakeWhisper([], fail=RuntimeError("CUDA out of memory"))
    with pytest.raises(asr.ModelError, match="Transcription failed: CUDA out of memory"):
        run(transcriber(fake), silence)


# --------------------------------------------------------------------------- loading models
@pytest.fixture
def clean_cache():
    asr._MODEL_CACHE.clear()
    yield
    asr._MODEL_CACHE.clear()


def test_models_are_loaded_once_per_configuration(settings, monkeypatch, clean_cache):
    import faster_whisper
    built = []

    class Model:
        def __init__(self, name, **kw):
            built.append((name, kw))

    monkeypatch.setattr(faster_whisper, "WhisperModel", Model)
    monkeypatch.setattr(asr, "is_model_cached", lambda m: True)
    statuses = []
    first = asr.load_whisper("small", settings, statuses.append)
    again = asr.load_whisper("small", settings)
    other = asr.load_whisper("base", settings)
    assert first is again and first is not other and len(built) == 2
    assert built[0][1] == {"device": "cpu", "compute_type": "int8", "cpu_threads": 0}
    assert statuses == ["Loading the speech model"]


def test_a_first_run_without_internet_explains_what_to_do(settings, monkeypatch, clean_cache):
    import faster_whisper

    def boom(*_a, **_k):
        raise OSError("Name or service not known")

    monkeypatch.setattr(faster_whisper, "WhisperModel", boom)
    monkeypatch.setattr(asr, "is_model_cached", lambda m: False)
    statuses = []
    with pytest.raises(asr.ModelError) as err:
        asr.load_whisper("small", settings, statuses.append)
    assert "first run needs internet" in str(err.value) and "HRC_MODEL" in str(err.value)
    assert "Downloading" in statuses[0]


def test_model_memory_errors_suggest_lower_memory_options(settings, monkeypatch, clean_cache):
    import faster_whisper

    def boom(*_a, **_k):
        raise RuntimeError("mkl_malloc: failed to allocate memory")

    monkeypatch.setattr(faster_whisper, "WhisperModel", boom)
    monkeypatch.setattr(asr, "is_model_cached", lambda m: False)
    with pytest.raises(asr.ModelError) as err:
        asr.load_whisper("small", settings)
    message = str(err.value)
    assert "model could not allocate enough memory" in message
    assert "--model tiny" in message and "HRC_CPU_THREADS=2" in message
    assert "paging file" in message
    assert "first run needs internet" not in message


def test_local_model_folders_count_as_cached(tmp_path):
    assert asr.is_model_cached(str(tmp_path)) is True
    assert asr.is_model_cached("definitely-not-a-real-model-name") is False


@pytest.mark.parametrize("compute, device, expected", [
    ("auto", "cpu", "int8"), ("auto", "cuda", "float16"), ("int8_float16", "cuda", "int8_float16"),
])
def test_compute_type(compute, device, expected):
    assert asr.resolve_compute_type(compute, device) == expected


def test_device_resolution():
    assert asr.resolve_device("cpu") == "cpu" and asr.resolve_device("cuda") == "cuda"
    assert asr.resolve_device("auto") in ("cpu", "cuda")


def test_read_wav_head(silence, tmp_path):
    audio = asr.read_wav_head(silence, seconds=1.0)
    assert audio.dtype == np.float32 and len(audio) == 16000 and float(np.abs(audio).max()) == 0.0
    loud = tmp_path / "loud.wav"
    with wave.open(str(loud), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(np.full(100, 32767, dtype=np.int16).tobytes())
    assert 0.99 < float(asr.read_wav_head(loud).max()) <= 1.0


# --------------------------------------------------------------------------- demo backend
def test_the_demo_backend_is_deterministic_and_stays_inside_the_clip(settings, silence):
    mock = asr.MockTranscriber(settings)
    a = mock.transcribe(silence, language_mode="hinglish", model="x", hints="", duration=10.0)
    b = mock.transcribe(silence, language_mode="hinglish", model="x", hints="", duration=10.0)
    assert [(w.text, w.start, w.end) for w in a.words] == [(w.text, w.start, w.end) for w in b.words]
    assert a.words and a.model == "demo" and a.language == "hi"
    assert all(0 <= w.start < w.end <= 10.0 for w in a.words)
    assert all(x.end <= y.start for x, y in zip(a.words, a.words[1:]))


def test_the_demo_backend_loops_its_script_for_long_clips_and_is_quiet_for_tiny_ones(settings, silence):
    mock = asr.MockTranscriber(settings)
    long = mock.transcribe(silence, language_mode="hinglish", model="x", hints="", duration=120.0)
    assert len(long.words) > len(asr.DEMO_SCRIPT)
    tiny = mock.transcribe(silence, language_mode="hinglish", model="x", hints="", duration=0.3)
    assert tiny.words == []


def test_the_demo_backend_can_be_cancelled(settings, silence):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        asr.MockTranscriber(settings).transcribe(silence, language_mode="hinglish", model="x", hints="",
                                                 duration=5.0, cancel=cancel)


def test_get_transcriber_follows_the_backend_setting(settings, monkeypatch):
    from dataclasses import replace
    assert isinstance(asr.get_transcriber(settings), asr.MockTranscriber)
    assert isinstance(asr.get_transcriber(replace(settings, asr_backend="faster-whisper")), asr.FasterWhisperTranscriber)
    monkeypatch.setenv("HRC_MOCK_DELAY", "not-a-number")
    assert asr.get_transcriber(settings).delay == 0.0
    monkeypatch.setenv("HRC_MOCK_DELAY", "0.5")
    assert asr.get_transcriber(settings).delay == 0.5
