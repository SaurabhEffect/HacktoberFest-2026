"""Devanagari -> Roman Hinglish: lexicon first, rules for everything else."""
import pytest

from reelcaptioner.asr import RawWord
from reelcaptioner.pipeline import build_words
from reelcaptioner.romanize import (apply_natural_case, convert_word, norm_key, parse_user_words,
                                    romanize_text, romanize_token, split_edges, transliterate)


# --------------------------------------------------------------------------- the rule engine alone
@pytest.mark.parametrize("devanagari, roman", [
    ("समझ", "samajh"),          # medial schwa kept, final one dropped
    ("बनता", "banta"),          # medial schwa deleted
    ("ज़िंदगी", "zindagi"),      # nukta + anusvara, no schwa deletion next to a nasal
    ("मेहनत", "mehnat"),
    ("गयी", "gayi"),            # hiatus: aa + ii typed the way people write it
    ("हुई", "hui"),
    ("पढ़ाई", "padhai"),
    ("ज्ञान", "gyaan"),         # the jna conjunct
    ("शर्मा", "sharma"),
    ("कृष्ण", "krishna"),       # conjunct-final consonants keep their trailing a
    ("सत्य", "satya"),
    ("मित्र", "mitra"),
    ("प्यार", "pyaar"),
    ("क्या", "kya"),
    ("मुझे", "mujhe"),
    ("ठीक", "theek"),
    ("सुंदर", "sundar"),
    ("संभव", "sambhav"),        # anusvara before a labial is an m
    ("पंजाब", "panjaab"),
    ("बाहर", "baahar"),
])
def test_rules_spell_words_the_way_people_type_them(devanagari, roman):
    assert transliterate(devanagari) == roman


def test_digits_and_empty_input():
    assert transliterate("२०२६") == "2026"
    assert transliterate("") == ""


# --------------------------------------------------------------------------- lexicon beats rules
@pytest.mark.parametrize("devanagari, roman", [
    ("हम", "hum"), ("नहीं", "nahi"), ("तो", "toh"), ("मैं", "main"), ("अच्छा", "accha"),
    ("ज़्यादा", "zyada"), ("क्योंकि", "kyunki"), ("बहन", "behen"),
    ("बस", "bas"),                       # "bas" (just/enough), not the vehicle, in Hinglish speech
])
def test_everyday_hindi_uses_the_common_spelling(devanagari, roman):
    assert convert_word(devanagari) == roman


@pytest.mark.parametrize("devanagari, roman", [
    ("वीडियो", "video"), ("लैपटॉप", "laptop"), ("रील्स", "reels"), ("फ्री", "free"),
    ("एडिट", "edit"), ("डाउनलोड", "download"),
])
def test_english_words_written_in_devanagari_come_back_as_english(devanagari, roman):
    assert convert_word(devanagari) == roman


@pytest.mark.parametrize("devanagari, roman", [
    ("एपीआई", "API"), ("यूपीआई", "UPI"), ("ओटीपी", "OTP"), ("एआई", "AI"),
])
def test_acronyms_stay_acronyms(devanagari, roman):
    assert convert_word(devanagari) == roman


def test_spelling_variants_share_one_lexicon_entry():
    # chandrabindu vs anusvara, nukta or not, zero-width joiner
    assert norm_key("हैं") == norm_key("हैँ")
    assert convert_word("ज़्यादा") == convert_word("ज्यादा")
    assert convert_word("क‍र") == convert_word("कर")


# --------------------------------------------------------------------------- user words
def test_parse_user_words_ignores_comments_and_blank_lines():
    table = parse_user_words("""
        # my brand names
        doston = dosto
        भाई=bhai   # trailing comment
        broken line without equals
        =nothing
        empty=
    """)
    assert table == {"doston": "dosto", norm_key("भाई"): "bhai"}


def test_user_words_beat_the_lexicon_and_the_rules():
    mine = parse_user_words("दोस्तों=dosto\nझकास=jhakaas")
    assert convert_word("दोस्तों", mine) == "dosto"
    assert convert_word("झकास", mine) == "jhakaas"
    assert convert_word("दोस्तों") == "doston"          # without them the lexicon answer stands


def test_user_words_can_fix_latin_words_too_case_insensitively():
    mine = parse_user_words("whisper=Whisper AI")
    assert romanize_token("whisper,", mine) == "Whisper AI,"


# --------------------------------------------------------------------------- tokens and text
def test_latin_text_is_left_alone_and_punctuation_survives():
    assert romanize_token("Hinglish,") == "Hinglish,"
    assert romanize_token('"hello!"') == '"hello!"'
    assert romanize_token("नमस्ते,") == "namaste,"
    assert romanize_token("(वीडियो)") == "(video)"


def test_danda_becomes_a_full_stop():
    assert romanize_token("है।") == "hai."
    assert romanize_text("ये सब free है। कोई API नहीं।") == "yeh sab free hai. koi API nahi."


def test_mixed_script_inside_one_token_is_converted_in_place():
    assert romanize_token("reels में") == "reels mein"
    assert romanize_token("video-वीडियो") == "video-video"
    assert romanize_text("reels में Hinglish") == "reels mein Hinglish"


def test_split_edges():
    assert split_edges('"नमस्ते!"') == ('"', "नमस्ते", '!"')
    assert split_edges("word") == ("", "word", "")


# --------------------------------------------------------------------------- casing
def test_sentence_case_pronoun_and_brands():
    out = apply_natural_case("so i use youtube and instagram. then whatsapp".split())
    assert out == ["So", "I", "use", "YouTube", "and", "Instagram.", "Then", "WhatsApp"]


def test_case_resets_after_sentence_end_but_not_after_a_comma():
    assert apply_natural_case(["haan,", "theek", "hai.", "chalo"]) == ["Haan,", "theek", "hai.", "Chalo"]


def test_a_capital_inside_a_word_survives_the_start_of_a_sentence():
    """"iPhone" at the head of a sentence must not become "IPhone", whether it is a known brand or the user's own."""
    assert apply_natural_case(["iphone", "hai."]) == ["iPhone", "hai."]               # a brand the app knows
    assert apply_natural_case(["eBay", "par", "dekho.", "macOS", "bhi"]) == ["eBay", "par", "dekho.", "macOS", "bhi"]
    assert apply_natural_case(["ho", "gaya.", "iOS", "hai"]) == ["Ho", "gaya.", "iOS", "hai"]
    mine = parse_user_words("aifon=iPhone\ne bay=eBay")
    assert [w.w for w in build_words([RawWord("aifon", 0, 1), RawWord("hai.", 1, 2)], mine)] == ["iPhone", "hai."]
    assert [w.w for w in build_words([RawWord("e", 0, 1), RawWord("bay", 1, 2)], mine)] == ["eBay"]


# --------------------------------------------------------------------------- pipeline.build_words
def test_build_words_keeps_timestamps_and_romanises():
    raw = [RawWord("नमस्ते", 0.4, 0.8), RawWord("दोस्तों,", 0.9, 1.4), RawWord("आज", 1.5, 1.7)]
    words = build_words(raw)
    assert [w.w for w in words] == ["Namaste", "doston,", "aaj"]
    assert [(w.s, w.e) for w in words] == [(0.4, 0.8), (0.9, 1.4), (1.5, 1.7)]


def test_build_words_splits_a_rule_that_expands_into_several_words():
    raw = [RawWord("tk", 2.0, 2.5)]
    words = build_words(raw, parse_user_words("tk=thank you"))
    assert [w.w for w in words] == ["Thank", "you"]
    assert words[0].s == pytest.approx(2.0)
    assert words[1].e == pytest.approx(2.5)
    assert words[0].e == pytest.approx(words[1].s)          # the span is shared, with no gap or overlap


def test_build_words_drops_empty_tokens_and_survives_reversed_times():
    raw = [RawWord("", 0, 1), RawWord("हाँ", 3.0, 2.0)]
    words = build_words(raw)
    assert len(words) == 1 and words[0].e >= words[0].s


# --------------------------------------------------------------------------- rules that name several words
def spans(words):
    return [(w.w, w.s, w.e) for w in words]


def test_a_rule_can_name_several_words_and_the_result_spans_their_time():
    raw = [RawWord("aaj", 0.5, 0.8), RawWord("insta", 1.0, 1.3), RawWord("gram", 1.3, 1.8), RawWord("par", 1.9, 2.1)]
    words = build_words(raw, parse_user_words("insta gram=Instagram"))
    assert spans(words) == [("Aaj", 0.5, 0.8), ("Instagram", 1.0, 1.8), ("par", 1.9, 2.1)]


def test_punctuation_around_a_multi_word_rule_is_kept_and_inner_punctuation_is_ignored():
    raw = [RawWord("(insta,", 1.0, 1.3), RawWord("gram),", 1.3, 1.8)]
    assert [w.w for w in build_words(raw, parse_user_words("insta gram=Instagram"))] == ["(Instagram),"]


def test_multi_word_rules_work_in_devanagari_and_ignore_spelling_variants():
    raw = [RawWord("इंस्टा", 1.0, 1.3), RawWord("ग्राम", 1.3, 1.8)]
    assert spans(build_words(raw, parse_user_words("इंस्टा ग्राम=Instagram"))) == [("Instagram", 1.0, 1.8)]
    zed = [RawWord("ज़ोमैटो", 0.0, 0.5), RawWord("डिलीवरी", 0.5, 1.0)]                 # a nukta in the audio's spelling
    assert [w.w for w in build_words(zed, parse_user_words("जोमैटो डिलीवरी=Zomato delivery"))] == ["Zomato", "delivery"]


def test_roman_multi_word_rules_ignore_case():
    raw = [RawWord("Insta", 1.0, 1.3), RawWord("GRAM", 1.3, 1.8)]
    assert [w.w for w in build_words(raw, parse_user_words("insta gram=Instagram"))] == ["Instagram"]


def test_the_longest_rule_wins_and_a_shorter_one_still_applies_when_the_longer_one_falls_short():
    rules = parse_user_words("insta=IG\ninsta gram=Instagram\ninsta gram reels=IG Reels")
    three = [RawWord("insta", 0, 1), RawWord("gram", 1, 2), RawWord("reels", 2, 3)]
    assert [w.w for w in build_words(three, rules)] == ["IG", "Reels"]                # "IG Reels" is split in two
    two = [RawWord("insta", 0, 1), RawWord("gram", 1, 2), RawWord("par", 2, 3)]
    assert [w.w for w in build_words(two, rules)] == ["Instagram", "par"]
    alone = [RawWord("insta", 0, 1), RawWord("yaar", 1, 2)]
    assert [w.w for w in build_words(alone, rules)] == ["IG", "yaar"]


def test_the_words_of_a_rule_must_be_next_to_each_other():
    raw = [RawWord("insta", 0, 1), RawWord("aur", 1, 2), RawWord("gram", 2, 3)]
    assert [w.w for w in build_words(raw, parse_user_words("insta gram=Instagram"))] == ["Insta", "aur", "gram"]


def test_spaces_in_a_rule_are_forgiven():
    assert parse_user_words("insta    gram  =  Instagram") == {"insta gram": "Instagram"}
    raw = [RawWord("insta", 0, 1), RawWord("gram", 1, 2)]
    assert [w.w for w in build_words(raw, parse_user_words("  Insta \t GRAM=Instagram"))] == ["Instagram"]


def test_a_rule_can_mix_scripts():
    raw = [RawWord("Reels", 0, 1), RawWord("में", 1, 2)]
    assert [w.w for w in build_words(raw, parse_user_words("reels में=Reels Mein"))] == ["Reels", "Mein"]


def test_a_danda_after_a_multi_word_rule_becomes_a_full_stop():
    raw = [RawWord("इंस्टा", 1.0, 1.3), RawWord("ग्राम।", 1.3, 1.8)]
    assert [w.w for w in build_words(raw, parse_user_words("इंस्टा ग्राम=Instagram"))] == ["Instagram."]


def test_a_phrase_is_never_joined_across_a_sentence_end():
    raw = [RawWord("insta.", 0.0, 0.4), RawWord("gram", 0.5, 0.9)]
    assert [w.w for w in build_words(raw, parse_user_words("insta gram=Instagram"))] == ["Insta.", "Gram"]
    comma = [RawWord("insta,", 0.0, 0.4), RawWord("gram", 0.5, 0.9)]
    assert [w.w for w in build_words(comma, parse_user_words("insta gram=Instagram"))] == ["Instagram"]


def test_a_phrase_is_never_joined_across_a_long_pause():
    rules = parse_user_words("insta gram=Instagram")
    far = [RawWord("insta", 0.0, 0.4), RawWord("gram", 4.0, 4.4)]            # two segments, seconds apart
    assert [w.w for w in build_words(far, rules)] == ["Insta", "gram"]
    near = [RawWord("insta", 0.0, 0.4), RawWord("gram", 0.9, 1.3)]            # an ordinary hesitation
    assert [w.w for w in build_words(near, rules)] == ["Instagram"]


# --------------------------------------------------------------------------- rules written the way the captions read
def test_a_rule_can_be_written_against_the_roman_spelling_of_a_devanagari_word():
    mine = parse_user_words("doston=dosto")
    assert romanize_token("दोस्तों,", mine) == "dosto,"
    assert romanize_token("दोस्तों") == "doston"                                # without the rule the lexicon stands
    words = build_words([RawWord("नमस्ते", 0, 1), RawWord("दोस्तों", 1, 2)], mine)
    assert [w.w for w in words] == ["Namaste", "dosto"]


def test_a_multi_word_rule_can_be_written_against_the_roman_spelling_too():
    mine = parse_user_words("aap ka=aapka")
    assert [w.w for w in build_words([RawWord("आप", 0, 1), RawWord("का", 1, 2)], mine)] == ["Aapka"]
    assert [w.w for w in build_words([RawWord("aap", 0, 1), RawWord("ka", 1, 2)], mine)] == ["Aapka"]


def test_a_rule_for_the_devanagari_beats_a_rule_for_its_roman_spelling():
    mine = parse_user_words("doston=dostoon\nदोस्तों=dosto")
    assert romanize_token("दोस्तों", mine) == "dosto"


def test_rules_never_chain():
    mine = parse_user_words("insta=IG\nig=Instagram\nदोस्तों=doston\ndoston=dosto")
    assert romanize_token("insta", mine) == "IG"                               # not "Instagram"
    assert romanize_token("दोस्तों", mine) == "doston"                         # not "dosto"
    raw = [RawWord("insta", 0, 1), RawWord("gram", 1, 2)]
    assert [w.w for w in build_words(raw, parse_user_words("insta gram=ig\nig=IG"))] == ["Ig"]


def test_without_multi_word_rules_nothing_is_merged():
    raw = [RawWord("insta", 0, 1), RawWord("gram", 1, 2)]
    assert [w.w for w in build_words(raw, parse_user_words("gram=GRAM"))] == ["Insta", "GRAM"]
    assert [w.w for w in build_words(raw, {})] == ["Insta", "gram"]
    assert [w.w for w in build_words(raw, None)] == ["Insta", "gram"]
