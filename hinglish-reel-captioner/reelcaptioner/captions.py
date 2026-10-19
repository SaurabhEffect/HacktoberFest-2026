"""Caption cards: styles, line layout, grouping words into cards, and edit re-timing.

Everything here is resolution independent. Layout is computed in a fixed reference space
(1000 px tall, same aspect ratio as the video), so the browser preview, the SRT/VTT export
and the burned-in render all break lines in exactly the same places.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from PIL import ImageFont

from .config import FONTS_DIR

REF_HEIGHT = 1000.0          # layout reference space (px)
SAFE_WIDTH = 0.86            # share of the video width captions may use
PAUSE_BREAK = 0.70           # a silence this long always starts a new card (s)
MAX_CARD_SECONDS = 3.4
MIN_WORD_SECONDS = 0.06
MIN_CARD_SECONDS = 0.45
TAIL_SECONDS = 0.30

FONT_FILES = {"bold": "Poppins-Bold.ttf", "medium": "Poppins-Medium.ttf"}

POSITIONS = ("auto", "top", "middle", "lower", "bottom")
CASES = ("natural", "upper", "lower")
PUNCTS = ("keep", "light", "none")
WEIGHTS = ("bold", "medium")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass
class Style:
    preset: str = "pumpkin"
    weight: str = "bold"
    font_size: float = 4.6        # em height as a percentage of the video height
    position: str = "auto"        # auto = lower third for vertical video, bottom for landscape
    case: str = "natural"
    punct: str = "light"
    max_words: int = 4
    lines: int = 2
    highlight: bool = True        # colour the word being spoken
    pop: bool = True              # tiny pop-in on every new card
    text_color: str = "#FFFFFF"
    highlight_color: str = "#FF8A2B"
    outline_color: str = "#17302B"
    outline: float = 0.14         # stroke width as a fraction of the em height
    shadow: float = 0.0
    box: bool = False             # solid box (in the outline colour) behind the text instead of a stroke

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]] = None) -> "Style":
        data = dict(data or {})
        base = asdict(cls())
        base.update(PRESETS.get(str(data.get("preset", "pumpkin")), PRESETS["pumpkin"]))
        # "custom" and "brand" are looks the editor built itself; every field arrives with them.
        base["preset"] = data.get("preset") if data.get("preset") in (*PRESETS, "custom", "brand") else "pumpkin"
        known = {f.name for f in fields(cls)}
        base.update({k: v for k, v in data.items() if k in known and k != "preset"})
        return cls(**_clean(base))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _num(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _clean(raw: Dict[str, Any]) -> Dict[str, Any]:
    d = Style()
    out = dict(raw)
    out["font_size"] = _num(raw.get("font_size"), d.font_size, 2.0, 9.0)
    out["max_words"] = int(_num(raw.get("max_words"), d.max_words, 1, 10))
    out["lines"] = int(_num(raw.get("lines"), d.lines, 1, 2))
    out["outline"] = _num(raw.get("outline"), d.outline, 0.0, 0.4)
    out["shadow"] = _num(raw.get("shadow"), d.shadow, 0.0, 0.3)
    for key, allowed, default in (("position", POSITIONS, d.position), ("case", CASES, d.case),
                                  ("punct", PUNCTS, d.punct), ("weight", WEIGHTS, d.weight)):
        out[key] = raw.get(key) if raw.get(key) in allowed else default
    for key in ("text_color", "highlight_color", "outline_color"):
        value = str(raw.get(key, ""))
        out[key] = value.upper() if HEX.match(value) else getattr(d, key)
    for key in ("highlight", "pop", "box"):
        out[key] = bool(raw.get(key, getattr(d, key)))
    return out


PRESETS: Dict[str, Dict[str, Any]] = {
    "pumpkin": dict(weight="bold", font_size=4.6, case="natural", max_words=4, highlight=True, pop=True,
                    text_color="#FFFFFF", highlight_color="#FF8A2B", outline_color="#17302B",
                    outline=0.14, shadow=0.0, box=False),
    "pop": dict(weight="bold", font_size=5.2, case="upper", max_words=3, highlight=True, pop=True,
                text_color="#FFFFFF", highlight_color="#FFE14D", outline_color="#000000",
                outline=0.16, shadow=0.04, box=False),
    "classic": dict(weight="medium", font_size=3.8, case="natural", max_words=7, highlight=False, pop=False,
                    text_color="#FFFFFF", highlight_color="#FFFFFF", outline_color="#000000",
                    outline=0.07, shadow=0.05, box=False),
    "box": dict(weight="bold", font_size=4.2, case="natural", max_words=5, highlight=True, pop=False,
                text_color="#FFFFFF", highlight_color="#7CF3B0", outline_color="#17302B",
                outline=0.18, shadow=0.0, box=True),
}
PRESET_LABELS = {"pumpkin": "Pumpkin", "pop": "Pop", "classic": "Classic", "box": "Box"}


def preset_catalog() -> List[Dict[str, Any]]:
    out = []
    for key in PRESETS:
        style = Style.from_dict({"preset": key}).to_dict()
        style["label"] = PRESET_LABELS[key]
        out.append(style)
    return out


@dataclass
class Word:
    w: str
    s: float
    e: float

    def to_dict(self) -> Dict[str, Any]:
        return {"w": self.w, "s": round(self.s, 3), "e": round(self.e, 3)}


@dataclass
class Card:
    id: int
    start: float
    end: float
    words: List[Word]
    lines: List[List[int]] = field(default_factory=list)
    scale: float = 1.0

    @property
    def text(self) -> str:
        return " ".join(w.w for w in self.words)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "start": round(self.start, 3), "end": round(self.end, 3),
                "words": [w.to_dict() for w in self.words], "lines": self.lines,
                "scale": round(self.scale, 4)}


def resolved_position(style: Style, width: int, height: int) -> str:
    if style.position != "auto":
        return style.position
    return "lower" if height > width else "bottom"


_EDGE_PUNCT = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)


def display_text(word: str, style: Style) -> str:
    """What viewers actually see: punctuation and case rules applied."""
    text = word
    if style.punct == "none":
        text = _EDGE_PUNCT.sub("", text) or ""
    elif style.punct == "light":
        text = re.sub(r"[.,;:…]+$", "", text)
        text = re.sub(r"^[.,;:]+", "", text)
    if style.case == "upper":
        text = text.upper()
    elif style.case == "lower":
        text = text.lower()
    return text.strip()


@lru_cache(maxsize=4)
def _measuring_font(weight: str) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS_DIR / FONT_FILES.get(weight, FONT_FILES["bold"])), 200)


@lru_cache(maxsize=8192)
def _advance(text: str, weight: str) -> float:
    """Advance width of ``text`` in em units."""
    return _measuring_font(weight).getlength(text) / 200.0


def _layout_space(style: Style, aspect: float) -> Tuple[float, float, float]:
    em = style.font_size / 100.0 * REF_HEIGHT
    safe = SAFE_WIDTH * aspect * REF_HEIGHT - 2 * style.outline * em
    return em, max(safe, em), _advance(" ", style.weight) * em


def fit_lines(widths: Sequence[float], space: float, max_lines: int, safe: float
              ) -> Tuple[List[List[int]], float]:
    """Split word widths over at most ``max_lines`` lines. Returns (lines, scale <= 1)."""
    n = len(widths)
    if n == 0:
        return [], 1.0

    def span(a: int, b: int) -> float:
        return sum(widths[a:b]) + space * max(0, b - a - 1)

    total = span(0, n)
    if total <= safe or max_lines <= 1 or n == 1:
        return [list(range(n))], min(1.0, safe / total) if total > 0 else 1.0
    # Pick the split with the narrowest widest line; when widths are within ~6% of each other,
    # prefer an even number of words per line, then a slightly longer bottom line.
    best_k, best_key, best_worst = 1, None, 0.0
    step = max(0.06 * safe, 1e-6)
    for k in range(1, n):
        first, second = span(0, k), span(k, n)
        worst = max(first, second)
        key = (int(worst / step), abs(k - (n - k)), 0 if second >= first else 1)
        if best_key is None or key < best_key:
            best_k, best_key, best_worst = k, key, worst
    scale = min(1.0, safe / best_worst) if best_worst > 0 else 1.0
    return [list(range(best_k)), list(range(best_k, n))], scale


def _widths(words: Sequence[Word], style: Style, em: float) -> Tuple[List[int], List[float]]:
    shown, widths = [], []
    for i, word in enumerate(words):
        text = display_text(word.w, style)
        if text:
            shown.append(i)
            widths.append(_advance(text, style.weight) * em)
    return shown, widths


def layout_card(card: Card, style: Style, aspect: float) -> None:
    em, safe, space = _layout_space(style, aspect)
    shown, widths = _widths(card.words, style, em)
    lines, scale = fit_lines(widths, space, style.lines, safe)
    card.lines = [[shown[j] for j in line] for line in lines]
    card.scale = scale


def layout_cards(cards: Sequence[Card], style: Style, aspect: float) -> None:
    for card in cards:
        layout_card(card, style, aspect)


def _fits(words: Sequence[Word], style: Style, aspect: float) -> bool:
    em, safe, space = _layout_space(style, aspect)
    _, widths = _widths(words, style, em)
    return fit_lines(widths, space, style.lines, safe)[1] >= 0.999


_SENTENCE_END = re.compile(r"[.?!…]+[\"')\]]*$")
_COMMA_END = re.compile(r"[,;:]+[\"')\]]*$")
_PUNCT_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)


def sanitize_words(words: Sequence[Word], duration: Optional[float] = None) -> List[Word]:
    """Drop empties, glue stray punctuation onto the previous word, make timings monotonic."""
    cleaned: List[Word] = []
    for word in words:
        text = (word.w or "").strip()
        if not text:
            continue
        if _PUNCT_ONLY.match(text) and cleaned:
            cleaned[-1].w += text
            cleaned[-1].e = max(cleaned[-1].e, word.e)
            continue
        cleaned.append(Word(text, float(word.s), float(word.e)))
    prev_end = 0.0
    for word in cleaned:
        word.s = max(word.s, prev_end, 0.0)
        if duration is not None:
            word.s = min(word.s, max(0.0, duration - MIN_WORD_SECONDS))
        word.e = max(word.e, word.s + MIN_WORD_SECONDS)
        if duration is not None:
            word.e = min(word.e, duration) if duration > word.s else word.s + MIN_WORD_SECONDS
        prev_end = word.e
    return cleaned


def _finish_timing(cards: List[Card], duration: Optional[float]) -> None:
    for i, card in enumerate(cards):
        card.start = card.words[0].s
        nxt = cards[i + 1].words[0].s if i + 1 < len(cards) else None
        end = card.words[-1].e
        if nxt is not None and nxt - end < TAIL_SECONDS + 0.05:
            end = nxt
        else:
            end += TAIL_SECONDS
        end = max(end, card.start + MIN_CARD_SECONDS)
        if nxt is not None:
            end = min(end, nxt)
        if duration is not None:
            end = min(end, duration)
        card.end = max(end, card.words[-1].e if duration is None else min(card.words[-1].e, end))
        card.id = i + 1


def group_words(words: Sequence[Word], style: Style, aspect: float,
                duration: Optional[float] = None) -> List[Card]:
    """Greedy grouping into readable cards (pauses, sentence ends, width, max words)."""
    words = sanitize_words(words, duration)
    groups: List[List[Word]] = []
    cur: List[Word] = []
    for word in words:
        if cur:
            brk = word.s - cur[-1].e > PAUSE_BREAK
            brk = brk or bool(_SENTENCE_END.search(cur[-1].w))
            brk = brk or len(cur) >= style.max_words
            brk = brk or (word.e - cur[0].s) > MAX_CARD_SECONDS
            brk = brk or (bool(_COMMA_END.search(cur[-1].w)) and len(cur) >= max(2, style.max_words - 1))
            if not brk and not _fits(cur + [word], style, aspect):
                brk = True
            if brk:
                groups.append(cur)
                cur = []
        cur.append(word)
    if cur:
        groups.append(cur)

    # Avoid a lonely last word when it can join the previous card without breaking the rules.
    if style.max_words >= 3:
        merged: List[List[Word]] = []
        for group in groups:
            if (merged and len(group) == 1 and len(merged[-1]) <= style.max_words
                    and not _SENTENCE_END.search(merged[-1][-1].w)
                    and group[0].s - merged[-1][-1].e <= PAUSE_BREAK
                    and _fits(merged[-1] + group, style, aspect)):
                merged[-1].extend(group)
            else:
                merged.append(group)
        groups = merged

    cards = [Card(0, g[0].s, g[-1].e, g) for g in groups]
    _finish_timing(cards, duration)
    layout_cards(cards, style, aspect)
    return cards


def flatten_words(cards: Sequence[Card]) -> List[Word]:
    return [Word(w.w, w.s, w.e) for card in cards for w in card.words]


def _distribute(tokens: Sequence[str], start: float, end: float) -> List[Word]:
    if not tokens:
        return []
    span = max(end - start, MIN_WORD_SECONDS * len(tokens))
    weights = [max(len(t), 2) for t in tokens]
    total = float(sum(weights))
    out, cursor = [], start
    for token, weight in zip(tokens, weights):
        step = span * weight / total
        out.append(Word(token, cursor, cursor + step))
        cursor += step
    return out


def retime_words(old: Sequence[Word], new_text: str, start: float, end: float) -> List[Word]:
    """Give edited text sensible word timings, keeping as much of the old timing as possible."""
    tokens = new_text.split()
    if not tokens:
        return []
    if len(tokens) == len(old):
        return [Word(t, o.s, o.e) for t, o in zip(tokens, old)]
    if not old:
        return _distribute(tokens, start, end)
    def key(s: str) -> str:
        return re.sub(r"\W+", "", s).casefold()

    lo = 0
    while lo < min(len(tokens), len(old)) and key(tokens[lo]) == key(old[lo].w):
        lo += 1
    hi = 0
    while (hi < min(len(tokens), len(old)) - lo
           and key(tokens[-1 - hi]) == key(old[-1 - hi].w)):
        hi += 1
    head = [Word(tokens[i], old[i].s, old[i].e) for i in range(lo)]
    tail = [Word(tokens[len(tokens) - hi + i], old[len(old) - hi + i].s, old[len(old) - hi + i].e)
            for i in range(hi)]
    middle = tokens[lo:len(tokens) - hi]
    if not middle:
        return head + tail
    span_start = head[-1].e if head else start
    span_end = tail[0].s if tail else end
    if span_end - span_start < MIN_WORD_SECONDS * len(middle):
        return _distribute(tokens, start, end)
    return head + _distribute(middle, span_start, span_end) + tail


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def normalize_cards(raw_cards: Sequence[Mapping[str, Any]], duration: float) -> List[Card]:
    """Validate cards coming from the editor and rebuild word timings from the text.

    Anything malformed (not an object, non-numeric or non-finite times, empty text) is skipped
    instead of raising, so a bad payload can never take the editor down.
    """
    cards: List[Card] = []
    for raw in raw_cards:
        if not isinstance(raw, Mapping):
            continue
        try:
            start = max(0.0, float(raw.get("start", 0)))
            end = min(duration, float(raw.get("end", start + 1)))
        except (TypeError, ValueError):
            continue
        if not _finite(start, end):
            continue
        old = []
        raw_words = raw.get("words")
        for w in raw_words if isinstance(raw_words, list) else []:
            try:
                word = Word(str(w["w"]), float(w["s"]), float(w["e"]))
            except (KeyError, TypeError, ValueError):
                continue
            if _finite(word.s, word.e):
                old.append(word)
        text = raw.get("text")
        text = str(text) if text is not None else " ".join(w.w for w in old)
        if end <= start:
            end = min(duration, start + MIN_CARD_SECONDS)
        words = retime_words(old, text, start, end)
        if not words:
            continue
        words = [Word(w.w, min(max(w.s, start), end), min(max(w.e, start), end)) for w in words]
        cards.append(Card(0, start, end, words))
    cards.sort(key=lambda c: c.start)
    for i, card in enumerate(cards):
        card.id = i + 1
        if i + 1 < len(cards):
            card.end = min(card.end, cards[i + 1].start)
        card.end = max(card.end, card.start + 0.05)
        for word in card.words:
            word.s = min(word.s, card.end)
            word.e = min(max(word.e, word.s), card.end)
    return cards


def cards_from_dicts(items: Sequence[Mapping[str, Any]]) -> List[Card]:
    cards = []
    for item in items:
        words = [Word(str(w["w"]), float(w["s"]), float(w["e"])) for w in item.get("words", [])]
        if words:
            cards.append(Card(int(item.get("id", len(cards) + 1)), float(item["start"]),
                              float(item["end"]), words, [list(line) for line in item.get("lines", [])],
                              float(item.get("scale", 1.0))))
    return cards
