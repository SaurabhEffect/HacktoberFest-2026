"""Styles, line fitting, grouping words into cards, and re-timing after edits."""
import math

import pytest

from reelcaptioner import captions as C
from reelcaptioner.captions import (Card, Style, Word, cards_from_dicts, display_text, fit_lines, group_words,
                                    normalize_cards, preset_catalog, resolved_position, retime_words,
                                    sanitize_words)

PORTRAIT = 9 / 16
LANDSCAPE = 16 / 9


# --------------------------------------------------------------------------- Style
def test_default_style_is_the_pumpkin_preset():
    style = Style.from_dict()
    assert style.preset == "pumpkin" and style.highlight and style.pop and not style.box
    assert Style.from_dict(None) == Style.from_dict({}) == style


def test_unknown_preset_falls_back_to_pumpkin():
    assert Style.from_dict({"preset": "nope"}).preset == "pumpkin"
    assert Style.from_dict({"preset": "../../etc"}).preset == "pumpkin"


def test_presets_apply_their_look():
    box = Style.from_dict({"preset": "box"})
    assert box.box and box.highlight_color == "#7CF3B0"
    classic = Style.from_dict({"preset": "classic"})
    assert not classic.highlight and classic.weight == "medium"
    assert Style.from_dict({"preset": "pop"}).case == "upper"


def test_explicit_fields_override_the_preset_and_custom_keeps_them():
    s = Style.from_dict({"preset": "custom", "font_size": 6.5, "text_color": "#112233", "box": True})
    assert (s.preset, s.font_size, s.text_color, s.box) == ("custom", 6.5, "#112233", True)
    s = Style.from_dict({"preset": "pop", "max_words": 2})
    assert s.preset == "pop" and s.max_words == 2 and s.case == "upper"


@pytest.mark.parametrize("field, raw, clean", [
    ("font_size", 99, 9.0), ("font_size", -5, 2.0), ("font_size", "abc", 4.6), ("font_size", "5.5", 5.5),
    ("max_words", 0, 1), ("max_words", 50, 10), ("max_words", "3", 3),
    ("lines", 5, 2), ("lines", 0, 1),
    ("outline", 9, 0.4), ("outline", -1, 0.0), ("shadow", 9, 0.3), ("shadow", -1, 0.0),
])
def test_numbers_are_clamped_and_garbage_gets_the_default(field, raw, clean):
    assert getattr(Style.from_dict({field: raw}), field) == pytest.approx(clean)


def test_colours_must_be_six_digit_hex():
    s = Style.from_dict({"text_color": "red", "highlight_color": "#abcdef", "outline_color": "#12"})
    assert s.text_color == "#FFFFFF"                       # default
    assert s.highlight_color == "#ABCDEF"                  # normalised to upper case
    assert s.outline_color == "#17302B"
    assert Style.from_dict({"text_color": "#00ff00; drop"}).text_color == "#FFFFFF"


def test_choice_fields_reject_unknown_values():
    s = Style.from_dict({"position": "diagonal", "case": "shout", "punct": "x", "weight": "black"})
    assert (s.position, s.case, s.punct, s.weight) == ("auto", "natural", "light", "bold")


def test_unknown_keys_are_ignored_including_the_old_box_opacity():
    s = Style.from_dict({"box_opacity": 0.4, "evil": "x", "preset": "box"})
    assert not hasattr(s, "box_opacity") and not hasattr(s, "evil")
    assert s.box


def test_preset_catalog_lists_the_four_looks_in_order():
    cat = preset_catalog()
    assert [p["preset"] for p in cat] == ["pumpkin", "pop", "classic", "box"]
    assert all(p["label"] and p["text_color"].startswith("#") for p in cat)


@pytest.mark.parametrize("position, size, expected", [
    ("auto", (1080, 1920), "lower"), ("auto", (1920, 1080), "bottom"), ("auto", (1080, 1080), "bottom"),
    ("top", (1080, 1920), "top"), ("middle", (1920, 1080), "middle"),
])
def test_auto_position_depends_on_orientation(position, size, expected):
    assert resolved_position(Style.from_dict({"position": position}), *size) == expected


# --------------------------------------------------------------------------- display rules
def test_light_punctuation_drops_trailing_marks_but_keeps_questions():
    style = Style.from_dict({"punct": "light"})
    assert display_text("doston,", style) == "doston"
    assert display_text("hai.", style) == "hai"
    assert display_text("kya?", style) == "kya?"
    assert display_text("wow!", style) == "wow!"


def test_punctuation_modes():
    assert display_text('"kya?"', Style.from_dict({"punct": "keep"})) == '"kya?"'
    assert display_text('"kya?"', Style.from_dict({"punct": "none"})) == "kya"
    assert display_text("...", Style.from_dict({"punct": "none"})) == ""


def test_case_modes():
    assert display_text("Hello", Style.from_dict({"case": "upper"})) == "HELLO"
    assert display_text("Hello", Style.from_dict({"case": "lower"})) == "hello"
    assert display_text("Hello", Style.from_dict({"case": "natural"})) == "Hello"


# --------------------------------------------------------------------------- fit_lines
def test_fit_lines_empty_and_single_line():
    assert fit_lines([], 10, 2, 500) == ([], 1.0)
    assert fit_lines([100, 120, 90], 10, 2, 500) == ([[0, 1, 2]], 1.0)


def test_fit_lines_balances_two_lines_and_never_scales_up():
    lines, scale = fit_lines([200, 200, 200, 200], 10, 2, 500)
    assert lines == [[0, 1], [2, 3]] and scale == 1.0


def test_fit_lines_scales_down_when_nothing_fits():
    lines, scale = fit_lines([600, 600], 10, 2, 500)
    assert lines == [[0], [1]] and scale == pytest.approx(500 / 600)
    one, scale1 = fit_lines([300, 300], 10, 1, 500)
    assert one == [[0, 1]] and scale1 == pytest.approx(500 / 610)


def test_fit_lines_prefers_an_even_word_split_when_widths_are_close():
    lines, _ = fit_lines([180, 190, 150, 170], 12, 2, 420)
    assert [len(line) for line in lines] == [2, 2]


# --------------------------------------------------------------------------- grouping
STYLE = Style.from_dict({"preset": "pumpkin"})


def _all_words(cards):
    return [w.w for c in cards for w in c.words]


def test_every_word_lands_in_exactly_one_card_in_order(words):
    ws = words("ek do teen chaar paanch chhe saat aath nau das gyarah barah")
    cards = group_words(ws, STYLE, PORTRAIT)
    assert _all_words(cards) == [w.w for w in ws]
    assert [c.id for c in cards] == list(range(1, len(cards) + 1))


def test_cards_never_overlap_and_stay_inside_the_clip(words):
    cards = group_words(words("aaj hum baat karenge ki kaise aap apne reels mein captions laga sakte ho"),
                        STYLE, PORTRAIT, duration=6.0)
    for a, b in zip(cards, cards[1:]):
        assert a.end <= b.start + 1e-9
    assert all(0 <= c.start < c.end <= 6.0 + 1e-9 for c in cards)
    assert all(c.start == c.words[0].s for c in cards)


def test_a_long_pause_starts_a_new_card(words):
    ws = words("ek do", start=0.0) + words("teen chaar", start=3.0)
    cards = group_words(ws, STYLE, PORTRAIT)
    assert [c.text for c in cards] == ["ek do", "teen chaar"]


def test_sentence_ends_start_a_new_card(words):
    cards = group_words(words("bas itna. phir baat karte hain"), STYLE, PORTRAIT)
    assert cards[0].text == "bas itna."


def test_max_words_is_respected_apart_from_rescuing_a_lonely_last_word(words):
    ws = words("a1 a2 a3 a4 a5 a6 a7 a8 a9 a10", step=0.35, dur=0.3)
    four = group_words(ws, Style.from_dict({"max_words": 4}), PORTRAIT)
    assert max(len(c.words) for c in four) <= 5
    assert [len(c.words) for c in four][:2] == [4, 4]
    one = group_words(ws, Style.from_dict({"max_words": 1}), PORTRAIT)
    assert all(len(c.words) == 1 for c in one) and len(one) == 10


def test_a_lonely_last_word_joins_the_previous_card(words):
    cards = group_words(words("ek do teen chaar paanch"), Style.from_dict({"max_words": 4}), PORTRAIT)
    assert [c.text for c in cards] == ["ek do teen chaar paanch"]


def test_a_card_is_never_longer_than_the_limit(words):
    cards = group_words(words("w1 w2 w3 w4 w5 w6 w7 w8", step=1.0, dur=0.9), Style.from_dict({"max_words": 8}), PORTRAIT)
    for card in cards:
        assert card.words[-1].e - card.words[0].s <= C.MAX_CARD_SECONDS + 1e-9
    assert len(cards) >= 3


def test_cards_break_early_instead_of_shrinking_the_text(words):
    ws = words("internationalization internationalization internationalization internationalization", step=0.5)
    cards = group_words(ws, Style.from_dict({"max_words": 6}), PORTRAIT)
    assert len(cards) > 1
    assert all(c.scale >= 0.999 or len(c.words) == 1 for c in cards)


def test_layout_matches_the_words_and_line_limit(words):
    for lines_allowed in (1, 2):
        style = Style.from_dict({"lines": lines_allowed, "max_words": 5})
        for card in group_words(words("kya baat hai yaar aaj kuch naya seekhte hain"), style, PORTRAIT):
            flat = [i for line in card.lines for i in line]
            assert flat == list(range(len(card.words)))
            assert 1 <= len(card.lines) <= lines_allowed
            assert 0 < card.scale <= 1.0


def test_hidden_punctuation_only_words_are_skipped_in_the_layout():
    style = Style.from_dict({"punct": "none"})
    card = Card(1, 0.0, 2.0, [Word("hello", 0, 0.5), Word("...", 0.5, 1.0), Word("world", 1.0, 1.5)])
    C.layout_card(card, style, PORTRAIT)
    assert [i for line in card.lines for i in line] == [0, 2]


def test_empty_input_gives_no_cards():
    assert group_words([], STYLE, PORTRAIT) == []
    assert group_words([Word("  ", 0, 1)], STYLE, PORTRAIT) == []


def test_landscape_fits_more_words_per_line_than_portrait(words):
    ws = words("kya baat hai yaar aaj kuch naya", step=0.3, dur=0.25)
    style = Style.from_dict({"max_words": 7, "lines": 1})
    wide = group_words(ws, style, LANDSCAPE)
    narrow = group_words(ws, style, PORTRAIT)
    assert len(wide) <= len(narrow)


# --------------------------------------------------------------------------- sanitising
def test_sanitize_glues_stray_punctuation_and_fixes_timing():
    raw = [Word(" hello ", 1.0, 1.5), Word("...", 1.6, 1.7), Word("", 2, 3), Word("world", 1.2, 1.3)]
    out = sanitize_words(raw)
    assert [w.w for w in out] == ["hello...", "world"]
    assert out[1].s >= out[0].e                              # overlapping start pushed past the previous word
    assert all(w.e >= w.s + C.MIN_WORD_SECONDS - 1e-9 for w in out)


def test_sanitize_clamps_to_the_duration():
    out = sanitize_words([Word("late", 9.9, 12.0), Word("later", 13.0, 14.0)], duration=10.0)
    assert all(0 <= w.s < 10.0 and w.e <= 10.0 + 1e-9 for w in out)


# --------------------------------------------------------------------------- retime_words
def _w(text, start=1.0, step=0.5, dur=0.4):
    return [Word(t, start + i * step, start + i * step + dur) for i, t in enumerate(text.split())]


def test_same_word_count_keeps_every_timestamp():
    old = _w("namaste doston aaj")
    new = retime_words(old, "Namaste dosto aaj", 1.0, 3.0)
    assert [w.w for w in new] == ["Namaste", "dosto", "aaj"]
    assert [(w.s, w.e) for w in new] == [(w.s, w.e) for w in old]


def test_inserting_a_word_keeps_the_neighbours_and_fills_the_gap():
    old = _w("hum baat karenge")
    new = retime_words(old, "hum aaj baat karenge", 1.0, 3.0)
    assert [w.w for w in new] == ["hum", "aaj", "baat", "karenge"]
    hum, aaj, baat = new[0], new[1], new[2]
    assert (hum.s, hum.e) == (old[0].s, old[0].e)
    assert (baat.s, baat.e) == (old[1].s, old[1].e)
    assert hum.e <= aaj.s <= aaj.e <= baat.s + 1e-9


def test_deleting_a_word_keeps_the_rest():
    old = _w("ek do teen chaar")
    new = retime_words(old, "ek teen chaar", 1.0, 3.0)
    assert [w.w for w in new] == ["ek", "teen", "chaar"]
    assert (new[0].s, new[-1].e) == (old[0].s, old[-1].e)


def test_replacing_the_whole_text_spreads_words_across_the_card():
    new = retime_words(_w("ek do"), "bilkul alag naya vaakya", 2.0, 4.0)
    assert [w.w for w in new] == ["bilkul", "alag", "naya", "vaakya"]
    assert new[0].s == pytest.approx(2.0) and new[-1].e == pytest.approx(4.0)
    assert all(a.e <= b.s + 1e-9 for a, b in zip(new, new[1:]))


def test_new_text_for_a_card_without_words_and_empty_text():
    assert [w.w for w in retime_words([], "ek do teen", 0, 3)] == ["ek", "do", "teen"]
    assert retime_words(_w("ek do"), "   ", 0, 1) == []


# --------------------------------------------------------------------------- normalize_cards
def test_normalize_sorts_clamps_numbers_the_cards_and_removes_overlaps():
    raw = [
        {"start": 4.0, "end": 9.0, "text": "last one", "words": []},
        {"start": -1.0, "end": 2.5, "text": "first one", "words": [{"w": "first", "s": 0, "e": 1}, {"w": "one", "s": 1, "e": 2}]},
        {"start": 2.0, "end": 3.0, "text": "middle", "words": []},
    ]
    cards = normalize_cards(raw, duration=5.0)
    assert [c.text for c in cards] == ["first one", "middle", "last one"]
    assert [c.id for c in cards] == [1, 2, 3]
    assert cards[0].start == 0.0 and cards[2].end == 5.0
    assert all(a.end <= b.start + 1e-9 for a, b in zip(cards, cards[1:]))
    assert all(w.s >= c.start and w.e <= c.end + 1e-9 for c in cards for w in c.words)


def test_normalize_drops_empty_and_broken_cards_without_raising():
    raw = [
        {"start": "x", "end": "y", "text": "bad numbers"},
        {"start": 1, "end": 2, "text": "   "},
        {"start": 1, "end": 2, "text": "ok", "words": [{"w": "ok"}, "nonsense", {"w": "ok", "s": "a", "e": 1}]},
        {"start": 5, "end": 5, "text": "zero length"},
    ]
    cards = normalize_cards(raw, duration=10.0)
    assert [c.text for c in cards] == ["ok", "zero length"]
    assert cards[1].end > cards[1].start


def test_normalize_retimes_edited_text_inside_the_card_window():
    raw = [{"start": 1.0, "end": 3.0, "text": "namaste doston aaj hum",
            "words": [{"w": "namaste", "s": 1.0, "e": 1.4}, {"w": "doston", "s": 1.5, "e": 2.0}]}]
    (card,) = normalize_cards(raw, duration=10.0)
    assert [w.w for w in card.words] == ["namaste", "doston", "aaj", "hum"]
    assert card.words[0].s == 1.0 and card.words[-1].e <= 3.0 + 1e-9


# --------------------------------------------------------------------------- serialisation
def test_cards_survive_a_round_trip_through_dicts(words):
    cards = group_words(words("namaste doston aaj hum baat karenge ki kaise"), STYLE, PORTRAIT)
    again = cards_from_dicts([c.to_dict() for c in cards])
    assert [c.text for c in again] == [c.text for c in cards]
    assert [c.lines for c in again] == [c.lines for c in cards]
    assert [round(c.scale, 3) for c in again] == [round(c.scale, 3) for c in cards]
    assert [c.start for c in again] == pytest.approx([c.start for c in cards], abs=1e-3)


def test_cards_from_dicts_skips_cards_without_words():
    assert cards_from_dicts([{"id": 1, "start": 0, "end": 1, "words": []}]) == []


def test_normalize_survives_non_objects_and_non_finite_numbers():
    raw = ["just a string", None, 7,
           {"start": float("nan"), "end": 2, "text": "nan start"},
           {"start": 0, "end": float("inf"), "text": "inf end"},
           {"start": 1, "end": 2, "text": "fine", "words": [{"w": "fine", "s": float("nan"), "e": 2}]}]
    cards = normalize_cards(raw, duration=10.0)
    assert "fine" in [c.text for c in cards]
    for card in cards:                                   # nothing non-finite may reach the JSON the editor gets
        values = [card.start, card.end] + [t for w in card.words for t in (w.s, w.e)]
        assert all(math.isfinite(v) for v in values)
