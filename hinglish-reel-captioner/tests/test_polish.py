"""The optional Ollama spelling pass: it may fix spellings and nothing else."""
import threading

import pytest

from reelcaptioner import polish
from reelcaptioner.captions import Card, Style, Word, group_words
from reelcaptioner.media import Cancelled


def make_cards(words, text="Namaste doston, aaj hum baat karenge. Yeh acha hai nai"):
    return group_words(words(text, step=0.5, dur=0.4), Style.from_dict({}), 9 / 16)


def snapshot(cards):
    return [[(w.w, w.s, w.e) for w in c.words] for c in cards]


# --------------------------------------------------------------------------- talking to Ollama
def test_list_models_reads_the_installed_names(fake_ollama):
    assert polish.list_models(fake_ollama.url) == ["llama3.2:3b", "gemma3:4b"]


def test_list_models_is_empty_when_ollama_is_not_running():
    assert polish.list_models("http://127.0.0.1:9") == []


@pytest.mark.parametrize("installed, wanted, expected", [
    (["llama3.2:3b", "gemma3:4b"], "", "gemma3:4b"),            # Gemma is preferred over Llama
    (["llama3.2:3b", "gemma3:4b"], "llama3.2:3b", "llama3.2:3b"),   # an explicit choice wins when installed
    (["llama3.2:3b"], "nonexistent", "llama3.2:3b"),            # ...and is ignored when it is not
    (["unknown-model:1b"], "", "unknown-model:1b"),             # anything is better than nothing
    ([], "gemma3", ""),
    (["qwen2.5:7b", "mistral:7b"], "", "qwen2.5:7b"),
])
def test_pick_model(installed, wanted, expected):
    assert polish.pick_model(installed, wanted) == expected


# --------------------------------------------------------------------------- the guard rails
@pytest.mark.parametrize("old, new, ok", [
    ("yeh acha hai", "yeh accha hai", True),            # a spelling fix
    ("yeh acha hai", "yeh accha hai!", True),           # punctuation does not count against it
    ("yeh acha hai", "yeh accha hai bhai", False),      # an extra word
    ("yeh acha hai", "yeh accha", False),               # a missing word
    ("yeh acha hai", "zzz qqq xxxx", False),            # a rewrite, not a spelling fix
    ("yeh acha hai", "", False),
    ("yeh acha hai", 42, False),
    ("...", "...", False),                              # nothing to compare
])
def test_acceptable_changes(old, new, ok):
    assert polish._acceptable(old, new) is ok


# --------------------------------------------------------------------------- polishing cards
def test_spelling_fixes_are_applied_and_timing_is_untouched(fake_ollama, words):
    fake_ollama.fixes = {"acha": "accha", "nai": "nahi", "doston,": "dosto,"}
    cards = make_cards(words)
    before = snapshot(cards)
    stats = polish.polish_cards(cards, fake_ollama.url, "gemma3:4b")
    after = snapshot(cards)
    assert stats["changed"] >= 1 and stats["failed"] == 0
    flat = [w for card in after for w, _, _ in card]
    assert "accha" in flat and "nahi" in flat and "dosto," in flat
    assert [(s, e) for card in after for _, s, e in card] == [(s, e) for card in before for _, s, e in card]
    assert [len(c) for c in after] == [len(c) for c in before]


def test_the_request_asks_for_deterministic_json(fake_ollama, words):
    polish.polish_cards(make_cards(words), fake_ollama.url, "gemma3:4b")
    body = fake_ollama.requests[0]
    assert body["model"] == "gemma3:4b" and body["format"] == "json" and body["stream"] is False
    assert body["options"]["temperature"] == 0
    assert "EXACTLY the same" in body["messages"][0]["content"]


def test_a_reply_with_the_wrong_number_of_words_is_ignored(fake_ollama, words):
    fake_ollama.mode = "wrong_count"
    cards = make_cards(words)
    before = snapshot(cards)
    stats = polish.polish_cards(cards, fake_ollama.url, "m")
    assert snapshot(cards) == before
    assert stats["changed"] == 0 and stats["kept"] == len(cards)


def test_a_word_that_contains_a_space_cannot_be_polished_and_is_left_alone(fake_ollama):
    """A "My words" rule can turn one heard word into two written ones ("ek dum"). The model's reply then splits
    into more tokens than the card has words, and writing it back word by word would drop one of them."""
    fake_ollama.fixes = {"dum": "dam"}
    cards = [Card(1, 0.0, 2.0, [Word("ek dum", 0.0, 1.0), Word("accha", 1.0, 2.0)])]
    before = snapshot(cards)
    stats = polish.polish_cards(cards, fake_ollama.url, "m")
    assert snapshot(cards) == before
    assert stats == {"changed": 0, "kept": 1, "failed": 0}


def test_a_reply_that_rewrites_everything_is_ignored(fake_ollama, words):
    fake_ollama.mode = "garbage"
    cards = make_cards(words, "ek do teen")
    before = snapshot(cards)
    assert polish.polish_cards(cards, fake_ollama.url, "m")["changed"] == 0
    assert snapshot(cards) == before


@pytest.mark.parametrize("mode", ["invalid_json", "http_error"])
def test_broken_replies_count_as_failed_batches_and_change_nothing(fake_ollama, words, mode):
    fake_ollama.mode = mode
    cards = make_cards(words)
    before = snapshot(cards)
    stats = polish.polish_cards(cards, fake_ollama.url, "m")
    assert stats["failed"] == 1 and stats["changed"] == 0 and stats["kept"] == len(cards)
    assert snapshot(cards) == before


def test_a_reply_wrapped_in_an_object_is_unwrapped(fake_ollama, words):
    fake_ollama.mode = "dict"
    fake_ollama.fixes = {"acha": "accha"}
    cards = make_cards(words)
    assert polish.polish_cards(cards, fake_ollama.url, "m")["changed"] >= 1


def test_unreachable_server_fails_every_batch_without_raising(words):
    cards = make_cards(words)
    before = snapshot(cards)
    stats = polish.polish_cards(cards, "http://127.0.0.1:9", "m", timeout=1)
    assert stats["failed"] == 1 and snapshot(cards) == before


def test_cards_are_sent_in_batches(fake_ollama):
    cards = [Card(i, i, i + 1, [Word("acha", i, i + 0.5), Word("hai", i + 0.5, i + 1)]) for i in range(30)]
    seen = []
    polish.polish_cards(cards, fake_ollama.url, "m", on_progress=seen.append)
    assert len(fake_ollama.requests) == 3                       # 12 + 12 + 6
    assert seen == sorted(seen) and seen[-1] == pytest.approx(1.0)


def test_cancelling_stops_before_the_next_request(fake_ollama):
    cards = [Card(i, i, i + 1, [Word("acha", i, i + 1)]) for i in range(30)]
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        polish.polish_cards(cards, fake_ollama.url, "m", cancel=cancel)
    assert fake_ollama.requests == []
