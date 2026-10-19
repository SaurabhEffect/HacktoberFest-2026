"""SRT / VTT / TXT / ASS output. The ASS file is what FFmpeg burns in, so its geometry is checked closely."""
import re

import pytest

from reelcaptioner import exporters as X
from reelcaptioner.captions import Card, Style, Word, group_words

SCRIPT = "Namaste doston, aaj hum baat karenge. Phir dekhte hain ki kaise captions lagte hain"
PORTRAIT = (720, 1280)


@pytest.fixture
def cards(words):
    return group_words(words(SCRIPT, start=0.5, step=0.4, dur=0.3), Style.from_dict({}), PORTRAIT[0] / PORTRAIT[1], 8.0)


def events(ass: str):
    """(start, end, tags, text) for every Dialogue line."""
    out = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            fields = line.split(",", 9)
            m = re.match(r"^(\{[^}]*\})(.*)$", fields[9])
            out.append((fields[1], fields[2], m.group(1), m.group(2)))
    return out


def pos_of(tags: str):
    m = re.search(r"\\pos\(([\d.]+),([\d.]+)\)", tags)
    return float(m.group(1)), float(m.group(2))


# --------------------------------------------------------------------------- SRT / VTT / TXT
def test_srt_has_numbered_cues_with_comma_timestamps(cards):
    srt = X.to_srt(cards, Style.from_dict({}), *PORTRAIT)
    blocks = srt.strip().split("\n\n")
    assert len(blocks) == len(cards)
    first = blocks[0].splitlines()
    assert first[0] == "1"
    assert re.fullmatch(r"\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}", first[1])
    assert first[1].startswith("00:00:00,500 -->")
    assert [b.splitlines()[0] for b in blocks] == [str(i) for i in range(1, len(blocks) + 1)]


def test_srt_text_follows_the_display_rules_and_line_breaks(cards):
    style = Style.from_dict({"preset": "pop"})                      # upper case, light punctuation
    srt = X.to_srt(cards, style, *PORTRAIT)
    assert "NAMASTE" in srt and "Namaste" not in srt
    assert "DOSTON," not in srt                                      # the trailing comma is hidden
    two_line = [b for b in srt.strip().split("\n\n") if len(b.splitlines()) == 4]
    assert two_line, "a four-word card should wrap onto two lines"


def test_vtt_header_and_dot_timestamps(cards):
    vtt = X.to_vtt(cards, Style.from_dict({}), *PORTRAIT)
    assert vtt.startswith("WEBVTT")
    assert re.search(r"\d\d:\d\d:\d\d\.\d{3} --> \d\d:\d\d:\d\d\.\d{3}", vtt)
    assert "," not in vtt.split("-->")[1].splitlines()[0]


def test_timestamps_roll_over_minutes_and_hours(words):
    cards = group_words(words("late word", start=3725.25), Style.from_dict({}), 9 / 16)
    assert "01:02:05,250 -->" in X.to_srt(cards, Style.from_dict({}))
    assert "01:02:05.250 -->" in X.to_vtt(cards, Style.from_dict({}))


def test_cards_with_nothing_to_show_are_left_out_of_srt(words):
    style = Style.from_dict({"punct": "none"})
    ws = words("hello world", start=0.0) + [Word("...", 5.0, 5.5)]
    cards = group_words(ws, style, 9 / 16)
    cards.append(Card(9, 6.0, 7.0, [Word("???", 6.0, 6.5)]))
    srt = X.to_srt(cards, style)
    assert srt.count("-->") == 1 and "hello" in srt


def test_txt_is_one_sentence_per_line_with_original_punctuation(cards):
    txt = X.to_txt(cards)
    assert txt.splitlines() == ["Namaste doston, aaj hum baat karenge.",
                                "Phir dekhte hain ki kaise captions lagte hain"]
    assert txt.endswith("\n")


# --------------------------------------------------------------------------- small helpers
def test_ass_colours_are_blue_green_red():
    assert X.ass_colour("#FF8A2B") == "&H002B8AFF"
    assert X.ass_colour("#000000", 128) == "&H80000000"
    assert X._override("#FF8A2B") == "&H2B8AFF&"


@pytest.mark.parametrize("size, cap, expected", [
    ((1080, 1920), 0, (1080, 1920)),
    ((1080, 1920), 720, (404, 720)),
    ((1920, 1080), 720, (1280, 720)),
    ((1081, 1921), 0, (1080, 1920)),            # H.264 needs even sizes
    ((640, 360), 720, (640, 360)),              # never upscales
])
def test_output_size(size, cap, expected):
    assert X.output_size(*size, max_height=cap) == expected


@pytest.mark.parametrize("position, size, expected", [
    ("lower", (1080, 1920), {"an": 2, "margin": 0.24}),
    ("bottom", (1080, 1920), {"an": 2, "margin": 0.13}),
    ("bottom", (1920, 1080), {"an": 2, "margin": 0.07}),
    ("top", (1080, 1920), {"an": 8, "margin": 0.11}),
    ("top", (1920, 1080), {"an": 8, "margin": 0.07}),
    ("middle", (1080, 1920), {"an": 5, "margin": 0.0}),
    ("auto", (1080, 1920), {"an": 2, "margin": 0.24}),
    ("auto", (1920, 1080), {"an": 2, "margin": 0.07}),
])
def test_position_spec(position, size, expected):
    assert X.position_spec(Style.from_dict({"position": position}), *size) == expected


# --------------------------------------------------------------------------- line anchors (shared with the preview)
def test_line_anchors_stack_at_the_line_pitch():
    em = 60.0
    for an in (2, 8, 5):
        anchors = X.line_anchors(an, 0.2, 1000.0, 3, em, box=False)
        assert [b - a for a, b in zip(anchors, anchors[1:])] == pytest.approx([X.LINE_PITCH * em] * 2)


def test_bottom_anchored_text_keeps_its_last_baseline_at_the_margin():
    em, h, margin = 50.0, 1280.0, 0.24
    (a1, a2) = X.line_anchors(2, margin, h, 2, em, box=False)
    last_baseline = a2 - X.ASS_BELOW_BASELINE * em
    css_line_bottom = last_baseline + (X.LINE_PITCH - X.BASELINE_FROM_TOP) * em
    assert css_line_bottom == pytest.approx(h * (1 - margin))


def test_top_anchored_text_starts_at_the_margin():
    em, h, margin = 50.0, 1000.0, 0.11
    (a1,) = X.line_anchors(8, margin, h, 1, em, box=False)
    first_baseline = a1 - X.ASS_BELOW_BASELINE * em
    assert first_baseline - X.BASELINE_FROM_TOP * em == pytest.approx(h * margin)


def test_middle_anchored_text_is_centred_on_the_frame():
    em, h = 40.0, 1000.0
    for n in (1, 2):
        anchors = X.line_anchors(5, 0.0, h, n, em, box=False)
        first_top = anchors[0] - X.ASS_BELOW_BASELINE * em - X.BASELINE_FROM_TOP * em
        block = n * X.LINE_PITCH * em
        assert first_top + block / 2 == pytest.approx(h / 2)


def test_box_style_uses_a_single_anchor():
    assert X.line_anchors(2, 0.24, 1000.0, 2, 40.0, box=True) == [760.0]
    assert X.line_anchors(8, 0.11, 1000.0, 2, 40.0, box=True) == [110.0]
    assert X.line_anchors(5, 0.0, 1000.0, 2, 40.0, box=True) == [500.0]


# --------------------------------------------------------------------------- ASS file
def test_ass_header_matches_the_canvas_and_style(cards):
    ass = X.to_ass(cards, Style.from_dict({}), *PORTRAIT)
    assert "PlayResX: 720" in ass and "PlayResY: 1280" in ass
    assert "YCbCr Matrix: None" in ass and "ScaledBorderAndShadow: yes" in ass
    style_line = next(line for line in ass.splitlines() if line.startswith("Style: Cap,"))
    f = style_line.split(",")
    em = 0.046 * 1280
    assert f[1] == "Poppins" and f[7] == "-1"                                   # bold flag
    assert float(f[2]) == pytest.approx(em * X.ASS_EM_TO_FONTSIZE, abs=0.01)    # font size
    assert float(f[16]) == pytest.approx(0.14 * em, abs=0.01)                  # outline width
    assert f[15] == "1" and f[18] == "2"                                         # outline style, bottom-centre
    assert f[5] == "&H002B3017"                                                  # outline colour is BGR


def test_medium_weight_uses_the_medium_family():
    ass = X.to_ass([Card(1, 0, 1, [Word("hi", 0, 1)])], Style.from_dict({"preset": "classic"}), *PORTRAIT)
    style_line = next(line for line in ass.splitlines() if line.startswith("Style: Cap,"))
    assert style_line.split(",")[1] == "Poppins Medium" and style_line.split(",")[7] == "0"


def test_highlight_makes_one_event_set_per_word_and_covers_the_card_exactly(words):
    style = Style.from_dict({"lines": 1})
    cards = group_words(words("ek do ye hai", step=0.5, dur=0.4), style, 9 / 16)
    assert len(cards) == 1 and len(cards[0].lines) == 1
    evs = events(X.to_ass(cards, style, *PORTRAIT))
    assert len(evs) == 4                                                          # one line x four word-states
    assert evs[0][0] == "0:00:00.50" and evs[-1][1] == X._ass_time(round(cards[0].end * 100))
    assert all(a[1] == b[0] for a, b in zip(evs, evs[1:]))                        # no gaps, no overlap
    lit = X._override(style.highlight_color)                                      # &H2B8AFF& for the pumpkin orange
    colored = [re.findall(re.escape("{\\1c" + lit + "}") + r"([^{]+)", text) for _, _, _, text in evs]
    assert colored == [["ek"], ["do"], ["ye"], ["hai"]]                           # exactly one word lit at a time


def test_without_highlight_a_card_is_one_event_per_line(cards):
    style = Style.from_dict({"preset": "classic"})
    ass = X.to_ass(cards, style, *PORTRAIT)
    evs = events(ass)
    assert "\\1c" not in ass
    assert len(evs) == sum(len(X._shown_lines(c, style)) for c in cards)


def test_every_line_gets_its_own_event_at_the_calculated_baseline(words):
    style = Style.from_dict({"preset": "pumpkin"})
    cards = group_words(words("Namaste doston aaj hum", step=0.4, dur=0.3), style, 9 / 16, 4.0)
    assert len(cards) == 1 and len(cards[0].lines) == 2
    evs = events(X.to_ass(cards, style, *PORTRAIT))
    em = 0.046 * 1280 * cards[0].scale
    expected = X.line_anchors(2, 0.24, 1280, 2, em, box=False)
    ys = sorted({pos_of(tags)[1] for _, _, tags, _ in evs})
    assert ys == pytest.approx(expected, abs=0.06)
    assert all(pos_of(tags)[0] == 360.0 for _, _, tags, _ in evs)                 # centred horizontally
    assert all(tags.startswith("{\\an2\\pos(") for _, _, tags, _ in evs)


@pytest.mark.parametrize("position, an", [("top", 8), ("middle", 5), ("lower", 2), ("bottom", 2)])
def test_positions_move_the_anchor_block(words, position, an):
    style = Style.from_dict({"position": position})
    cards = group_words(words("Namaste doston aaj hum", step=0.4, dur=0.3), style, 9 / 16, 4.0)
    ys = [pos_of(t)[1] for _, _, t, _ in events(X.to_ass(cards, style, *PORTRAIT))]
    mid = sum(ys) / len(ys) / 1280
    if position == "top":
        assert mid < 0.25
    elif position == "middle":
        assert 0.4 < mid < 0.6
    else:
        assert mid > 0.7


def test_pop_in_only_on_the_first_state_of_a_card(words):
    style = Style.from_dict({"preset": "pumpkin", "lines": 1})
    cards = group_words(words("aaj hum baat", step=0.5, dur=0.4), style, 9 / 16)
    evs = events(X.to_ass(cards, style, *PORTRAIT))
    assert "\\t(0,110," in evs[0][2] and "\\fscx86.0" in evs[0][2]
    assert all("\\t(" not in e[2] for e in evs[1:])
    still = Style.from_dict({"preset": "pumpkin", "lines": 1, "pop": False})
    assert all("\\t(" not in e[2] for e in events(X.to_ass(cards, still, *PORTRAIT)))


def test_cards_that_had_to_shrink_carry_their_scale(words):
    style = Style.from_dict({"lines": 1, "max_words": 1})
    cards = group_words(words("pneumonoultramicroscopicsilicovolcanoconiosis"), style, 9 / 16)
    assert cards[0].scale < 0.99
    tags = events(X.to_ass(cards, Style.from_dict({"lines": 1, "max_words": 1, "pop": False}), *PORTRAIT))[0][2]
    assert f"\\fscx{cards[0].scale * 100:.1f}" in tags


def test_box_style_is_one_multi_line_event_with_a_solid_box(words):
    style = Style.from_dict({"preset": "box"})
    cards = group_words(words("Namaste doston aaj hum baat", step=0.4, dur=0.3), style, 9 / 16, 4.0)
    ass = X.to_ass(cards, style, *PORTRAIT)
    style_line = next(line for line in ass.splitlines() if line.startswith("Style: Cap,"))
    f = style_line.split(",")
    assert f[15] == "3"                                                           # BorderStyle 3 = opaque box
    assert f[5] == "&H002B3017"                                                   # box colour is not transparent
    evs = events(ass)
    assert len(evs) == len(cards[0].words)                                        # one event per spoken word
    assert all("\\N" in text for _, _, _, text in evs) or len(cards[0].lines) == 1
    assert all(pos_of(tags)[1] == pytest.approx(0.76 * 1280) for _, _, tags, _ in evs)


def test_text_cannot_inject_override_tags(words):
    cards = group_words(words("{\\an8}hack \\fs500x {oops}", step=0.5), Style.from_dict({"lines": 1}), 9 / 16)
    evs = events(X.to_ass(cards, Style.from_dict({"lines": 1, "highlight": False}), *PORTRAIT))
    body = " ".join(text for _, _, _, text in evs)
    assert "{" not in body and "}" not in body and "\\" not in body.replace("\\N", "")


def test_empty_and_zero_length_cards_make_no_events():
    zero = Card(1, 2.0, 2.0, [Word("x", 2.0, 2.0)])
    hidden = Card(2, 3.0, 4.0, [Word("...", 3.0, 3.5)])
    ass = X.to_ass([zero, hidden], Style.from_dict({"punct": "none"}), *PORTRAIT)
    assert events(ass) == []


def test_ass_times_are_centiseconds():
    assert X._ass_time(0) == "0:00:00.00"
    assert X._ass_time(6000 + 150) == "0:01:01.50"
    assert X._ass_time(360000 * 2 + 1) == "2:00:00.01"
