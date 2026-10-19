"""SRT, WebVTT, ASS and plain-text exporters. All of them honour the same display rules."""
from __future__ import annotations

import re
from typing import Dict, List, Sequence, Tuple

from .captions import (Card, Style, display_text, layout_cards, resolved_position)

# libass sizes fonts by (winAscent + winDescent). For the bundled Poppins files that is
# 1135 + 627 = 1762 units per 1000-unit em, so ASS "Fontsize" = em * 1.762 (measured, see tests).
ASS_EM_TO_FONTSIZE = 1.762
ASS_FAMILY = {"bold": ("Poppins", -1), "medium": ("Poppins Medium", 0)}

# Vertical metrics shared with the browser preview (all in em). Every line of an outline-style card is
# its own ASS event placed at an exact baseline, so the stack is tight and identical in both places.
LINE_PITCH = 1.22          # distance between baselines (preview: CSS line-height)
BASELINE_FROM_TOP = 0.96   # CSS line box: top -> baseline for line-height 1.22 and Poppins
ASS_BELOW_BASELINE = 0.63  # libass \an2: anchor -> baseline (measured, see tests)
BOX_LINE_PITCH = 1.762     # solid-box style keeps libass' native stacking so the boxes touch

_SENTENCE_END = re.compile(r"[.?!…]+[\"')\]]*$")


def position_spec(style: Style, width: int, height: int) -> Dict[str, float]:
    """ASS alignment (2 bottom, 5 middle, 8 top) and the margin as a share of the height."""
    pos = resolved_position(style, width, height)
    portrait = height > width
    if pos == "top":
        return {"an": 8, "margin": 0.11 if portrait else 0.07}
    if pos == "middle":
        return {"an": 5, "margin": 0.0}
    if pos == "lower":
        return {"an": 2, "margin": 0.24}
    return {"an": 2, "margin": 0.13 if portrait else 0.07}


def output_size(width: int, height: int, max_height: int = 0) -> Tuple[int, int]:
    """Even dimensions (H.264 needs them), optionally capped to ``max_height``."""
    if max_height and height > max_height:
        width, height = round(width * max_height / height), max_height
    return max(2, width - width % 2), max(2, height - height % 2)


def _shown_lines(card: Card, style: Style) -> List[List[Tuple[int, str]]]:
    lines = []
    for line in card.lines:
        shown = [(i, display_text(card.words[i].w, style)) for i in line]
        shown = [(i, t) for i, t in shown if t]
        if shown:
            lines.append(shown)
    return lines


def card_text(card: Card, style: Style, joiner: str = "\n") -> str:
    return joiner.join(" ".join(t for _, t in line) for line in _shown_lines(card, style))


def _prepare(cards: Sequence[Card], style: Style, width: int, height: int) -> None:
    layout_cards(cards, style, width / float(height))


def _stamp(seconds: float, sep: str) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(cards: Sequence[Card], style: Style, width: int = 1080, height: int = 1920) -> str:
    _prepare(cards, style, width, height)
    out, n = [], 0
    for card in cards:
        text = card_text(card, style)
        if not text:
            continue
        n += 1
        out.append(f"{n}\n{_stamp(card.start, ',')} --> {_stamp(card.end, ',')}\n{text}\n")
    return "\n".join(out)


def to_vtt(cards: Sequence[Card], style: Style, width: int = 1080, height: int = 1920) -> str:
    _prepare(cards, style, width, height)
    out = ["WEBVTT\n"]
    for card in cards:
        text = card_text(card, style)
        if text:
            out.append(f"{_stamp(card.start, '.')} --> {_stamp(card.end, '.')}\n{text}\n")
    return "\n".join(out)


def to_txt(cards: Sequence[Card]) -> str:
    sentences, cur = [], []
    for card in cards:
        for word in card.words:
            cur.append(word.w)
            if _SENTENCE_END.search(word.w):
                sentences.append(" ".join(cur))
                cur = []
    if cur:
        sentences.append(" ".join(cur))
    return "\n".join(sentences) + "\n"


def _rgb(hex_color: str) -> Tuple[int, int, int]:
    return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)


def ass_colour(hex_color: str, alpha: int = 0) -> str:
    r, g, b = _rgb(hex_color)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _override(hex_color: str) -> str:
    r, g, b = _rgb(hex_color)
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _cs(seconds: float) -> int:
    return max(0, int(round(seconds * 100)))


def _ass_time(cs: int) -> str:
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def _line_markup(line: List[Tuple[int, str]], active: int, style: Style) -> str:
    parts = []
    for idx, text in line:
        text = _escape(text)
        if idx == active:
            parts.append("{\\1c%s}%s{\\1c%s}" % (_override(style.highlight_color), text, _override(style.text_color)))
        else:
            parts.append(text)
    return " ".join(parts)


def line_anchors(an: int, margin: float, height: float, n_lines: int, em_eff: float, box: bool) -> List[float]:
    """Y anchors (ASS \\an2 for outline style, \\an{an} for box style) for each line of a card."""
    if box:
        return [height * (1 - margin) if an == 2 else height * margin if an == 8 else height / 2.0]
    pitch = LINE_PITCH * em_eff
    if an == 2:
        last_base = height * (1 - margin) - (LINE_PITCH - BASELINE_FROM_TOP) * em_eff
    elif an == 8:
        last_base = height * margin + BASELINE_FROM_TOP * em_eff + (n_lines - 1) * pitch
    else:
        last_base = height / 2.0 - n_lines * pitch / 2.0 + BASELINE_FROM_TOP * em_eff + (n_lines - 1) * pitch
    return [last_base - (n_lines - 1 - i) * pitch + ASS_BELOW_BASELINE * em_eff for i in range(n_lines)]


def to_ass(cards: Sequence[Card], style: Style, width: int, height: int) -> str:
    _prepare(cards, style, width, height)
    em = style.font_size / 100.0 * height
    family, bold = ASS_FAMILY.get(style.weight, ASS_FAMILY["bold"])
    spec = position_spec(style, width, height)
    border_style = 3 if style.box else 1
    outline_px = max(0.0, style.outline * em)
    shadow_px = max(0.0, style.shadow * em)
    header = [
        "[Script Info]", "; Made locally by Hinglish Reel Captioner", "ScriptType: v4.00+",
        f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Cap,%s,%.2f,%s,%s,%s,%s,%d,0,0,0,100,100,0,0,%d,%.2f,%.2f,%d,0,0,%d,1" % (
            family, em * ASS_EM_TO_FONTSIZE, ass_colour(style.text_color), ass_colour(style.text_color),
            ass_colour(style.outline_color), ass_colour("#000000", 128), bold,
            border_style, outline_px, shadow_px, 2, 0),
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events: List[str] = []
    for card in cards:
        lines = _shown_lines(card, style)
        order = [idx for line in lines for idx, _ in line]
        if not order:
            continue
        start, end = _cs(card.start), _cs(card.end)
        if end <= start:
            continue
        if style.highlight and len(order) > 1:
            starts = [start]
            for idx in order[1:]:
                starts.append(min(max(_cs(card.words[idx].s), starts[-1]), end))
        else:
            starts = [start]
            order = order[:1]
        base = card.scale * 100.0
        em_eff = em * card.scale
        an = int(spec["an"])
        anchors = line_anchors(an, spec["margin"], height, len(lines), em_eff, style.box)
        for k, active in enumerate(order):
            ev_start = starts[k]
            ev_end = starts[k + 1] if k + 1 < len(starts) else end
            if ev_end <= ev_start:
                continue
            scale_tags = ""
            if style.pop and k == 0:
                scale_tags = "\\fscx%.1f\\fscy%.1f\\t(0,110,\\fscx%.1f\\fscy%.1f)" % (
                    base * 0.86, base * 0.86, base, base)
            elif card.scale < 0.999:
                scale_tags = "\\fscx%.1f\\fscy%.1f" % (base, base)
            active_idx = active if style.highlight else -1
            if style.box:
                tags = "{\\an%d\\pos(%.1f,%.1f)%s}" % (an, width / 2.0, anchors[0], scale_tags)
                body = "\\N".join(_line_markup(line, active_idx, style) for line in lines)
                events.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s%s" % (
                    _ass_time(ev_start), _ass_time(ev_end), tags, body))
            else:
                for line, y in zip(lines, anchors):
                    tags = "{\\an2\\pos(%.1f,%.1f)%s}" % (width / 2.0, y, scale_tags)
                    events.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s%s" % (
                        _ass_time(ev_start), _ass_time(ev_end), tags, _line_markup(line, active_idx, style)))
    return "\n".join(header + events) + "\n"
