"""ffprobe / ffmpeg wrappers, against small generated files."""
import subprocess
import threading
import wave

import pytest

from reelcaptioner import media
from reelcaptioner.media import Cancelled, MediaError

from .helpers import ffmpeg, needs_ffmpeg

pytestmark = needs_ffmpeg


# --------------------------------------------------------------------------- probe
def test_probe_a_video(clip):
    info = media.probe(clip)
    assert info["has_video"] and info["has_audio"]
    assert (info["width"], info["height"]) == (270, 480)
    assert info["duration"] == pytest.approx(6.0, abs=0.15)
    assert info["fps"] == pytest.approx(15.0, abs=0.1)
    assert info["video_codec"] == "h264" and info["audio_codec"] == "aac"
    assert info["size_bytes"] == clip.stat().st_size


def test_probe_audio_only(voice_note):
    info = media.probe(voice_note)
    assert not info["has_video"] and info["has_audio"]
    assert (info["width"], info["height"]) == (0, 0)
    assert info["duration"] == pytest.approx(4.0, abs=0.1)


def test_probe_swaps_width_and_height_for_rotated_phone_videos(clip, tmp_path):
    out = tmp_path / "rotated.mp4"
    try:
        ffmpeg("-display_rotation", "90", "-i", clip, "-c", "copy", out)
    except subprocess.CalledProcessError:
        pytest.skip("this FFmpeg cannot write a display rotation")
    info = media.probe(out)
    assert (info["width"], info["height"]) == (480, 270)


def test_probe_rejects_files_without_audio(silent_video):
    with pytest.raises(MediaError, match="no audio track"):
        media.probe(silent_video)


def test_probe_rejects_things_that_are_not_media(tmp_path):
    junk = tmp_path / "notes.mp4"
    junk.write_text("this is not a video")
    with pytest.raises(MediaError, match="does not look like a video or audio file"):
        media.probe(junk)
    with pytest.raises(MediaError):
        media.probe(tmp_path / "missing.mp4")


@pytest.mark.parametrize("value, expected", [
    ("30000/1001", 29.97), ("25/1", 25.0), ("0/0", 0.0), ("24", 24.0), ("abc", 0.0), (None, 0.0), ("5/0", 0.0),
])
def test_frame_rate_parsing(value, expected):
    assert media._fps(value) == pytest.approx(expected, abs=0.01)


# --------------------------------------------------------------------------- conversions
def test_extract_audio_makes_16khz_mono_wav(clip, tmp_path):
    out = tmp_path / "audio.wav"
    seen = []
    media.extract_audio(clip, out, duration=6.0, on_progress=seen.append)
    with wave.open(str(out), "rb") as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
        assert w.getnframes() / 16000 == pytest.approx(6.0, abs=0.2)
    assert seen and seen == sorted(seen) and seen[-1] <= 1.0


def test_make_preview_is_a_small_h264_copy(clip, tmp_path):
    out = tmp_path / "preview.mp4"
    media.make_preview(clip, out, media.probe(clip))
    info = media.probe(out)
    assert info["video_codec"] == "h264" and info["audio_codec"] == "aac"
    assert max(info["width"], info["height"]) <= 960 and info["width"] % 2 == 0


def test_make_preview_downsizes_large_videos(tmp_path):
    big = tmp_path / "big.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=1280x720:r=5:d=1", "-f", "lavfi", "-i", "sine=duration=1",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", big)
    out = tmp_path / "preview.mp4"
    media.make_preview(big, out, media.probe(big))
    assert media.probe(out)["width"] == 960


def test_make_preview_for_audio_is_aac_only(voice_note, tmp_path):
    out = tmp_path / "preview.m4a"
    media.make_preview(voice_note, out, media.probe(voice_note))
    info = media.probe(out)
    assert not info["has_video"] and info["audio_codec"] == "aac"


# --------------------------------------------------------------------------- run_ffmpeg
def test_ffmpeg_errors_become_friendly_messages(tmp_path):
    with pytest.raises(MediaError, match="ffmpeg could not process this file"):
        media.run_ffmpeg(["-i", tmp_path / "does-not-exist.mp4", tmp_path / "out.mp4"])


def test_cancelling_stops_ffmpeg(clip, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        media.run_ffmpeg(["-i", clip, tmp_path / "out.mp4"], duration=6.0, cancel=cancel)


def test_tools_status_reports_what_this_machine_can_do():
    status = media.tools_status()
    assert status["ffmpeg"] and status["ffprobe"]
    assert status["version"].startswith("ffmpeg version")
    assert isinstance(status["libass"], bool)


def test_a_missing_ffmpeg_is_explained(monkeypatch):
    monkeypatch.delenv("HRC_FFMPEG", raising=False)
    monkeypatch.setattr(media.shutil, "which", lambda _name: None)
    with pytest.raises(MediaError, match="Install FFmpeg"):
        media.ffmpeg_bin()


def test_ffmpeg_can_be_pointed_at_with_an_env_var(monkeypatch):
    monkeypatch.setenv("HRC_FFMPEG", "/opt/somewhere/ffmpeg")
    assert media.ffmpeg_bin() == "/opt/somewhere/ffmpeg"
