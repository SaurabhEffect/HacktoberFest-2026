"""Burning captions in with FFmpeg + libass, and checking where the pixels actually land."""
import subprocess

import numpy as np
import pytest
from PIL import Image

from reelcaptioner import media, render
from reelcaptioner.captions import Style, Word, group_words
from reelcaptioner.media import Cancelled, MediaError

from .helpers import needs_ffmpeg, needs_libass

pytestmark = [needs_ffmpeg, needs_libass]

BG = np.array([0x6B, 0x7F, 0x79])          # the flat clip's colour


def build(words, style, aspect, duration=3.0):
    ws = [Word(t, 0.5 + i * 0.4, 0.5 + i * 0.4 + 0.3) for i, t in enumerate(words.split())]
    return group_words(ws, style, aspect, duration)


def render_flat(flat_clip, tmp_path, style, words="Namaste doston, aaj hum", max_height=0):
    info = media.probe(flat_clip)
    cards = build(words, style, info["width"] / info["height"], info["duration"])
    job = tmp_path / "job"
    job.mkdir(exist_ok=True)
    out = render.render_video(job, flat_clip, info, cards, style, max_height=max_height)
    return out, info, cards


def frame_at(video, seconds, tmp_path, name="frame.png"):
    png = tmp_path / name
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(seconds), "-i", str(video), "-frames:v", "1", str(png)],
                   check=True)
    return np.asarray(Image.open(png).convert("RGB")).astype(int)


def caption_bbox(frame):
    """Normalised (x0, y0, x1, y1) of everything that is not the flat background."""
    h, w, _ = frame.shape
    changed = np.abs(frame - BG).max(axis=2) > 40
    ys, xs = np.where(changed)
    assert len(xs), "no caption pixels were found in the frame"
    return np.array([xs.min() / w, ys.min() / h, (xs.max() + 1) / w, (ys.max() + 1) / h])


# --------------------------------------------------------------------------- the file
def test_render_keeps_size_audio_and_duration(flat_clip, tmp_path):
    out, info, _ = render_flat(flat_clip, tmp_path, Style.from_dict({}))
    made = media.probe(out)
    assert (made["width"], made["height"]) == (info["width"], info["height"])
    assert made["video_codec"] == "h264" and made["has_audio"]
    assert made["duration"] == pytest.approx(info["duration"], abs=0.3)
    assert (tmp_path / "job" / "captions.ass").read_text(encoding="utf-8").startswith("[Script Info]")
    assert {p.name for p in (tmp_path / "job" / "fonts").iterdir()} >= {"Poppins-Bold.ttf", "Poppins-Medium.ttf"}


def test_render_can_cap_the_output_height(flat_clip, tmp_path):
    out, _, _ = render_flat(flat_clip, tmp_path, Style.from_dict({}), max_height=320)
    made = media.probe(out)
    assert (made["width"], made["height"]) == (180, 320)


def test_progress_is_reported(flat_clip, tmp_path):
    info = media.probe(flat_clip)
    cards = build("ek do teen", Style.from_dict({}), 9 / 16)
    seen = []
    render.render_video(tmp_path, flat_clip, info, cards, Style.from_dict({}), on_progress=seen.append)
    assert seen and seen[-1] <= 1.0 and seen == sorted(seen)


def test_audio_only_files_get_a_branded_background(voice_note, tmp_path):
    info = media.probe(voice_note)
    cards = build("Namaste doston aaj hum", Style.from_dict({}), 9 / 16, info["duration"])
    out = render.render_video(tmp_path, voice_note, info, cards, Style.from_dict({}), max_height=480)
    made = media.probe(out)
    assert (made["width"], made["height"]) == (270, 480) and made["has_video"] and made["has_audio"]
    assert made["duration"] == pytest.approx(4.0, abs=0.3)
    frame = frame_at(out, 0.2, tmp_path)                       # before the first caption: pure background
    assert np.abs(frame[5, 5] - np.array([0x17, 0x30, 0x2B])).max() <= 6


def test_square_and_landscape_canvases_for_audio_only_files(voice_note, tmp_path):
    info = media.probe(voice_note)
    for canvas, size in (("square", (480, 480)), ("landscape", (854, 480))):
        cards = build("ek do", Style.from_dict({}), size[0] / size[1], info["duration"])
        out = render.render_video(tmp_path, voice_note, info, cards, Style.from_dict({}), max_height=480,
                                  canvas=canvas, output_name=f"{canvas}.mp4")
        made = media.probe(out)
        assert abs(made["width"] - size[0]) <= 2 and made["height"] == 480


def test_render_explains_a_missing_subtitle_renderer(flat_clip, tmp_path, monkeypatch):
    monkeypatch.setattr(render, "tools_status", lambda: {"libass": False})
    with pytest.raises(MediaError, match="libass"):
        render_flat(flat_clip, tmp_path, Style.from_dict({}))


def test_render_can_be_cancelled(flat_clip, tmp_path):
    import threading
    cancel = threading.Event()
    cancel.set()
    info = media.probe(flat_clip)
    cards = build("ek do teen", Style.from_dict({}), 9 / 16)
    with pytest.raises(Cancelled):
        render.render_video(tmp_path, flat_clip, info, cards, Style.from_dict({}), cancel=cancel)


# --------------------------------------------------------------------------- where the caption lands
# These bounds come from measuring the same captions in the browser preview (see README, "How the preview
# matches the render"): the preview and the burn-in agree to within a fraction of a percent of the frame.
@pytest.mark.parametrize("position, y0, y1", [
    ("lower", 0.652, 0.766),
    ("top", 0.114, 0.229),
    ("middle", 0.448, 0.562),
    ("bottom", 0.762, 0.877),
])
def test_caption_lands_where_the_preview_puts_it(flat_clip, tmp_path, position, y0, y1):
    out, _, _ = render_flat(flat_clip, tmp_path, Style.from_dict({"position": position}))
    x0_, y0_, x1_, y1_ = caption_bbox(frame_at(out, 1.1, tmp_path))
    assert (x0_ + x1_) / 2 == pytest.approx(0.5, abs=0.01)                 # centred
    assert y0_ == pytest.approx(y0, abs=0.015) and y1_ == pytest.approx(y1, abs=0.015)
    assert x0_ > 0.05 and x1_ < 0.95                                        # inside the safe area


def test_text_never_leaves_the_frame_even_with_a_very_long_word(flat_clip, tmp_path):
    style = Style.from_dict({"lines": 1, "max_words": 1})
    out, _, cards = render_flat(flat_clip, tmp_path, style, words="Supercalifragilisticexpialidocious")
    assert cards[0].scale < 1.0
    x0, _, x1, _ = caption_bbox(frame_at(out, 1.0, tmp_path))
    assert x0 >= 0.02 and x1 <= 0.98


def test_the_active_word_is_drawn_in_the_highlight_colour(flat_clip, tmp_path):
    style = Style.from_dict({"preset": "pumpkin", "lines": 1})
    out, _, _ = render_flat(flat_clip, tmp_path, style, words="ek do teen")
    # "do" is spoken 0.9-1.2 s: orange pixels must exist, and white ones for the other words
    frame = frame_at(out, 1.0, tmp_path)
    orange = (np.abs(frame - np.array([0xFF, 0x8A, 0x2B])).max(axis=2) < 40).sum()
    white = (np.abs(frame - np.array([255, 255, 255])).max(axis=2) < 40).sum()
    assert orange > 80 and white > 80


def test_outline_style_has_round_dark_edges_around_light_text(flat_clip, tmp_path):
    out, _, _ = render_flat(flat_clip, tmp_path, Style.from_dict({"preset": "pop"}))
    frame = frame_at(out, 1.1, tmp_path)
    dark = (frame.max(axis=2) < 25).sum()
    assert dark > 300                                                      # the black outline is really there


BOX = np.array([0x17, 0x30, 0x2B])


def box_pixels(frame, tol):
    return np.abs(frame - BOX).max(axis=2) <= tol


def test_a_one_line_box_is_one_solid_colour_with_no_stripes(flat_clip, tmp_path):
    """Regression: libass draws one box per colour run, and translucent boxes left a dark band at each seam."""
    style = Style.from_dict({"preset": "box", "lines": 1})
    out, _, cards = render_flat(flat_clip, tmp_path, style, words="Namaste doston")
    assert len(cards[0].lines) == 1
    frame = frame_at(out, 1.0, tmp_path)               # "doston" is lit, so the box has a colour-run boundary
    ys, xs = np.where(box_pixels(frame, 6))
    assert len(xs) > 2000
    top, bottom, left, right = ys.min(), ys.max(), xs.min(), xs.max()
    # the top padding row and the left padding column are text-free: they must be box-coloured end to end
    # (stay a few pixels away from letters and edges, where 4:2:0 chroma subsampling blurs colours)
    assert box_pixels(frame[top + 3:top + 4, left + 2:right - 1], 6).all()
    assert box_pixels(frame[top + 2:bottom - 1, left + 1:left + 2], 6).all()
    # the highlighted word is mint, the others white
    mint = (np.abs(frame - np.array([0x7C, 0xF3, 0xB0])).max(axis=2) < 40).sum()
    white = (np.abs(frame - np.array([255, 255, 255])).max(axis=2) < 40).sum()
    assert mint > 60 and white > 60


def test_a_two_line_box_covers_both_lines_in_one_solid_block(flat_clip, tmp_path):
    style = Style.from_dict({"preset": "box"})
    out, _, cards = render_flat(flat_clip, tmp_path, style, words="Namaste doston aaj hum baat")
    assert len(cards[0].lines) == 2
    frame = frame_at(out, 1.1, tmp_path)
    assert box_pixels(frame, 6).sum() > 8000                              # a big, flat, opaque area
    x0, y0, x1, y1 = caption_bbox(frame)
    assert (x0, x1) == pytest.approx((0.168, 0.832), abs=0.015)
    assert (y0, y1) == pytest.approx((0.604, 0.768), abs=0.015)
    # the widest line's box reaches the full width and the narrower one is centred under it
    ys, xs = np.where(box_pixels(frame, 6))
    narrow_row = frame[int(y1 * frame.shape[0]) - 4]                       # a row inside the lower line's box
    cols = np.where(np.abs(narrow_row - BOX).max(axis=1) <= 6)[0]
    assert (cols.min() + cols.max()) / 2 / frame.shape[1] == pytest.approx(0.5, abs=0.02)
