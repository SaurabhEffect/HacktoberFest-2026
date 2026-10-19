"""Turn raw ASR words into clean, romanised, timed words ready for grouping into cards."""
from __future__ import annotations

from itertools import chain
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .asr import RawWord
from .captions import Word
from .romanize import HAS_DEVANAGARI, SENTENCE_END, apply_natural_case, romanize_token, split_edges, user_key

# A rule that names several words only joins words spoken together: a longer gap means they are two phrases.
MAX_PHRASE_GAP = 1.0

Phrase = Tuple[Tuple[str, ...], str]          # the words of a rule, and the rule's own key in the table


def _names(text: str, core: str) -> Tuple[str, ...]:
    """The spellings a rule can use for one word: as Whisper wrote it, then as it would be captioned."""
    if not core:
        return ()
    names = [user_key(core)]
    if HAS_DEVANAGARI.search(core):
        spoken = user_key(split_edges(romanize_token(text))[1])
        if spoken not in names:
            names.append(spoken)
    return tuple(names)


def _merge_phrases(raw: Sequence[RawWord], user_words: Mapping[str, str]) -> List[RawWord]:
    """Join the neighbouring words that one rule names together (``insta gram=Instagram``).

    Whisper hands the words over one at a time and a rule is looked up per word, so a rule that names several
    words would never match. Here every run of neighbours that spells such a rule becomes one word spanning
    their time, and the rule then applies to it like to any other. The longest rule wins, and a run is never
    joined across the end of a sentence or across a long pause.
    """
    phrases: Dict[str, List[Phrase]] = {}
    for key in user_words:
        parts = tuple(key.split())
        if len(parts) > 1:
            phrases.setdefault(parts[0], []).append((parts, key))
    if not phrases:
        return list(raw)

    edges = [split_edges(item.text) for item in raw]
    names = [_names(item.text, core) for item, (_, core, _) in zip(raw, edges)]
    merged: List[RawWord] = []
    i = 0
    while i < len(raw):
        options = sorted(chain.from_iterable(phrases.get(name, ()) for name in names[i]), key=lambda p: -len(p[0]))
        for parts, key in options:
            j = i + len(parts)
            if (j <= len(raw)
                    and all(part in names[i + n] for n, part in enumerate(parts))
                    and not any(SENTENCE_END.search(edges[n][2]) for n in range(i, j - 1))
                    and all(raw[n + 1].start - raw[n].end <= MAX_PHRASE_GAP for n in range(i, j - 1))):
                text = edges[i][0] + key + edges[j - 1][2]
                merged.append(RawWord(text, raw[i].start, raw[j - 1].end, min(w.prob for w in raw[i:j])))
                i = j
                break
        else:
            merged.append(raw[i])
            i += 1
    return merged


def build_words(raw: Sequence[RawWord], user_words: Optional[Mapping[str, str]] = None) -> List[Word]:
    """Romanise every word (keeping its timestamps) and apply sentence / brand casing.

    A word that expands into several words (for example a user rule ``tk=thank you``) shares
    its time span between the parts in proportion to their length. The reverse also holds: a rule
    that names several words (``insta gram=Instagram``) makes them one word spanning their time.
    """
    if user_words:
        raw = _merge_phrases(raw, user_words)
    tokens: List[str] = []
    spans: List[tuple] = []
    for item in raw:
        parts = romanize_token(item.text, user_words).split()
        if not parts:
            continue
        start, end = float(item.start), max(float(item.end), float(item.start))
        if len(parts) == 1:
            tokens.append(parts[0])
            spans.append((start, end))
            continue
        weights = [max(len(p), 2) for p in parts]
        total = float(sum(weights))
        cursor = start
        for part, weight in zip(parts, weights):
            step = (end - start) * weight / total
            tokens.append(part)
            spans.append((cursor, cursor + step))
            cursor += step
    tokens = apply_natural_case(tokens)
    return [Word(token, s, e) for token, (s, e) in zip(tokens, spans)]
