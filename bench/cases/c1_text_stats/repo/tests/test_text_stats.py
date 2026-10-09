import pytest

from text_stats import TextStats, analyse, reading_minutes, sentence_count, top_words, words


def test_words_keep_apostrophes_and_ignore_case():
    assert words("Don't panic, DON'T.") == ["don't", "panic", "don't"]


def test_sentence_count():
    assert sentence_count("Hi. There!") == 2
    assert sentence_count("Wait... what?") == 2
    assert sentence_count("no punctuation") == 1
    assert sentence_count("   ") == 0


def test_top_words_breaks_ties_alphabetically():
    assert top_words("b a b a c", n=2) == [("a", 2), ("b", 2)]
    with pytest.raises(ValueError):
        top_words("x", n=0)


def test_reading_minutes():
    assert reading_minutes("word " * 300) == 1.5
    with pytest.raises(ValueError):
        reading_minutes("x", wpm=0)


def test_analyse():
    assert analyse("Hello there. Hello again!") == TextStats(4, 3, 2, 0.0)
