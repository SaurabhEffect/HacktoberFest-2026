"""Devanagari -> Roman (Hinglish) conversion tuned for social-media captions.

Whisper writes Hindi in Devanagari. Creators want the way people actually type Hinglish
("kya baat hai yaar"), not a scholarly scheme. The converter works word by word so every
word keeps the timestamp Whisper gave it:

1. ``user words``  (the "My words" panel) win over everything,
2. a built-in lexicon covers the most frequent Hindi words and English loanwords,
3. everything else goes through a rule-based transliterator with Hindi schwa deletion.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Mapping, Optional, Tuple

from .lexicon import BRAND_CASE, HINDI_WORDS, LOANWORDS

NUKTA = "़"
VIRAMA = "्"
ANUSVARA = "ं"
CHANDRABINDU = "ँ"
VISARGA = "ः"
INVISIBLE = ("‌", "‍")

# Devanagari block without the two danda marks (they are punctuation, handled separately).
DEVANAGARI_RUN = re.compile("[ऀ-ॣ०-ॿ]+")
HAS_DEVANAGARI = re.compile("[ऀ-ॿ]")
SENTENCE_END = re.compile(r"[.?!।॥]+[\"')\]]*$")

CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh",
    "स": "s", "ह": "h", "ळ": "l",
}
NUKTA_CONSONANTS = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "d", "ढ": "dh", "फ": "f", "य": "y"}
LABIALS = {"प", "फ", "ब", "भ", "म"}

INDEPENDENT_VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e", "ॲ": "a",
}
MATRAS = {
    "ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}
DIGITS = {ord(d): str(i) for i, d in enumerate("०१२३४५६७८९")}

# Conjunct-final consonants that keep a trailing "a": mitra, satya, vishwa, krishna, shukla.
KEEP_FINAL_A = set("रयवणलन")

# vowel + independent vowel = hiatus, written the way people type it (gayi, hui, jaye, kiye).
HIATUS = {
    "aa": {"ee": "ai", "e": "aye", "o": "ao", "aa": "aa", "u": "au", "oo": "au", "i": "ayi"},
    "a": {"ee": "ayi", "e": "aye", "o": "ao", "aa": "aya", "i": "ayi"},
    "u": {"aa": "ua", "ee": "ui", "e": "ue", "o": "uo"},
    "i": {"e": "iye", "aa": "iya", "ee": "iyi"},
    "ee": {"e": "eeye", "aa": "eeya"},
    "o": {"ee": "oi", "e": "oye", "aa": "oya"},
    "e": {"ee": "ei"},
}


def norm_key(word: str) -> str:
    """Spelling variants that mean the same word share one key."""
    w = unicodedata.normalize("NFC", word)
    for ch in INVISIBLE:
        w = w.replace(ch, "")
    return w.replace(CHANDRABINDU, ANUSVARA).replace(NUKTA, "")


def _parse_lexicon(raw: str) -> Dict[str, str]:
    table: Dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        variants, value = line.rsplit("=", 1)
        for variant in variants.split("|"):
            variant = variant.strip()
            if variant and not any(ch.isspace() for ch in variant):
                table.setdefault(norm_key(variant), value.strip())
    return table


_LOAN = _parse_lexicon(LOANWORDS)
_HINDI = _parse_lexicon(HINDI_WORDS)


def parse_user_words(text: str) -> Dict[str, str]:
    """Parse the 'My words' box: one ``source=target`` per line, ``#`` starts a comment.

    The source can be several words (``insta gram=Instagram``): they only match when they sit next to each other.
    """
    table: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        source, target = line.split("=", 1)
        source, target = " ".join(source.split()), target.strip()
        if source and target:
            table[user_key(source)] = target
    return table


def user_key(word: str) -> str:
    """The form in which a rule and a transcript word are compared: no case, no spelling variants of Devanagari."""
    return norm_key(word).casefold()


class _Unit:
    """One syllable-ish piece: consonant cluster + vowel (+ optional nasal)."""

    __slots__ = ("cons", "last", "n_cons", "vowel", "inherent", "nasal", "indep", "conjunct", "visarga")

    def __init__(self) -> None:
        self.cons = ""
        self.last = ""        # last Devanagari consonant of the cluster
        self.n_cons = 0
        self.vowel = ""
        self.inherent = False
        self.nasal = False
        self.indep = False
        self.conjunct = False
        self.visarga = False


def _parse_units(word: str) -> List[_Unit]:
    units: List[_Unit] = []
    pending: List[str] = []   # romanised consonants joined by virama
    pending_chars: List[str] = []
    i, n = 0, len(word)

    def flush(vowel: str, inherent: bool) -> None:
        unit = _Unit()
        unit.cons = "".join(pending)
        unit.last = pending_chars[-1]
        unit.n_cons = len(pending_chars)
        unit.conjunct = len(pending_chars) > 1
        unit.vowel = vowel
        unit.inherent = inherent
        units.append(unit)
        pending.clear()
        pending_chars.clear()

    while i < n:
        ch = word[i]
        if ch in CONSONANTS:
            roman = CONSONANTS[ch]
            if i + 1 < n and word[i + 1] == NUKTA:
                roman = NUKTA_CONSONANTS.get(ch, roman)
                i += 1
            if ch == "ञ" and pending_chars and pending_chars[-1] == "ज":
                pending[-1] = "gy"        # ज्ञ -> gy (gyaan)
                pending_chars.append(ch)
            else:
                pending.append(roman)
                pending_chars.append(ch)
            i += 1
            nxt = word[i] if i < n else ""
            if nxt == VIRAMA:
                i += 1
                if i >= n:                 # word ends in a halant: no vowel at all
                    flush("", False)
                continue
            if nxt in MATRAS:
                flush(MATRAS[nxt], False)
                i += 1
            else:
                flush("a", True)
        elif ch in INDEPENDENT_VOWELS:
            unit = _Unit()
            unit.vowel = INDEPENDENT_VOWELS[ch]
            unit.indep = True
            units.append(unit)
            i += 1
        elif ch in (ANUSVARA, CHANDRABINDU):
            if units:
                units[-1].nasal = True
            else:
                unit = _Unit()
                unit.cons, unit.nasal = "n", True
                units.append(unit)
            i += 1
        elif ch == VISARGA:
            if units:
                units[-1].visarga = True
            i += 1
        else:                              # stray nukta, zero-width joiner, unknown mark
            i += 1
    return units


def _merge_hiatus(units: List[_Unit]) -> List[_Unit]:
    merged: List[_Unit] = []
    for unit in units:
        prev = merged[-1] if merged else None
        if unit.indep and prev is not None and prev.vowel:
            combo = HIATUS.get(prev.vowel, {}).get(unit.vowel)
            if combo is None:
                combo = prev.vowel + unit.vowel
            prev.vowel, prev.inherent = combo, False
            prev.nasal = prev.nasal or unit.nasal
            prev.visarga = prev.visarga or unit.visarga
            continue
        merged.append(unit)
    return merged


def _delete_schwas(units: List[_Unit]) -> None:
    """Hindi schwa deletion: drop the final schwa, then medial ones right to left."""
    last = units[-1]
    if last.inherent and not last.indep:
        if not (last.conjunct and last.last in KEEP_FINAL_A):
            last.vowel, last.inherent = "", False
    for i in range(len(units) - 2, 0, -1):
        unit, prev, nxt = units[i], units[i - 1], units[i + 1]
        if not unit.inherent or unit.n_cons != 1 or unit.nasal:
            continue
        if not prev.vowel or prev.nasal:       # keeps zindagi, avoids 3-consonant pile-ups
            continue
        if not nxt.vowel or nxt.n_cons > 1:
            continue
        unit.vowel, unit.inherent = "", False


def _assemble(units: List[_Unit], word: str) -> str:
    out: List[str] = []
    last_index = len(units) - 1
    for idx, unit in enumerate(units):
        vowel = unit.vowel
        if idx == last_index and unit.cons:
            if vowel == "aa":
                vowel = "a"
            elif vowel == "ee":
                vowel = "i"
            elif vowel == "oo":
                vowel = "u"
            elif vowel == "e" and unit.nasal:
                vowel = "ei"
        nasal = ""
        if unit.nasal:
            nasal = "n"
            nxt = units[idx + 1] if idx < last_index else None
            if nxt is not None and nxt.cons and _starts_with_labial(word, nxt):
                nasal = "m"
        out.append(unit.cons + vowel + nasal + ("h" if unit.visarga else ""))
    return "".join(out)


def _starts_with_labial(word: str, unit: _Unit) -> bool:
    return unit.cons[:1] in {"p", "b", "m"}


def transliterate(word: str) -> str:
    """Rule-based Devanagari -> Roman for one word (no lexicon)."""
    word = unicodedata.normalize("NFC", word)
    if all(ch in "०१२३४५६७८९" for ch in word):
        return word.translate(DIGITS)
    units = _parse_units(word)
    if not units:
        return ""
    units = _merge_hiatus(units)
    _delete_schwas(units)
    return _assemble(units, word)


def convert_word(word: str, user_words: Optional[Mapping[str, str]] = None) -> str:
    """Romanise one bare Devanagari word using user words, then lexicon, then rules."""
    key = norm_key(word)
    if user_words and key in user_words:
        return user_words[key]
    if key in _LOAN:
        return _LOAN[key]
    if key in _HINDI:
        return _HINDI[key]
    return transliterate(word)


_EDGE = re.compile(r"^([\"'(\[{“‘«¡¿]*)(.*?)([\"')\]}”’».,;:!?…।॥-]*)$", re.S)


def split_edges(token: str) -> Tuple[str, str, str]:
    m = _EDGE.match(token)
    return (m.group(1), m.group(2), m.group(3)) if m else ("", token, "")


def _fix_marks(text: str) -> str:
    return text.replace("॥", ".").replace("।", ".")


def _rule_for(user_words: Optional[Mapping[str, str]], text: str) -> Optional[str]:
    return user_words.get(user_key(text)) if user_words and text else None


def romanize_token(token: str, user_words: Optional[Mapping[str, str]] = None) -> str:
    """Romanise one Whisper word. Latin text is left alone, punctuation is kept.

    A "My words" rule is looked up by the word as Whisper wrote it and, for Devanagari, a second time by the Roman
    spelling that came out, so a rule can be written the way the captions read (``doston=dosto``).
    """
    lead, core, trail = split_edges(token)
    rule = _rule_for(user_words, core)
    if rule is None and HAS_DEVANAGARI.search(core):
        core = DEVANAGARI_RUN.sub(lambda m: convert_word(m.group(0), user_words), core)
        rule = _rule_for(user_words, core)
    return _fix_marks(lead) + (core if rule is None else rule) + _fix_marks(trail)


def romanize_text(text: str, user_words: Optional[Mapping[str, str]] = None) -> str:
    return " ".join(romanize_token(t, user_words) for t in text.split())


def apply_natural_case(tokens: List[str]) -> List[str]:
    """Sentence case, 'I', and the usual casing for brand names ("youtube" -> "YouTube")."""
    out: List[str] = []
    start = True
    for token in tokens:
        lead, core, trail = split_edges(token)
        lowered = core.casefold()
        if lowered in BRAND_CASE:
            core = BRAND_CASE[lowered]
        elif lowered == "i" and core == "i":
            core = "I"
        # A capital inside the word ("iPhone", "eBay", "macOS") is somebody's chosen spelling: leave it alone.
        if start and core and core[0].isalpha() and core == core.lower():
            core = core[0].upper() + core[1:]
        token = lead + core + trail
        out.append(token)
        if core or trail:
            start = bool(SENTENCE_END.search(token))
    return out
