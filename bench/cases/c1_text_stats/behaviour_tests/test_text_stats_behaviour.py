import text_stats


def test_analyse_a_paragraph():
    text = "The cat sat. The cat ran! Did the dog see it?"
    stats = text_stats.analyse(text)
    assert stats.words == 11
    assert stats.unique_words == 8
    assert stats.sentences == 3


def test_top_words():
    assert text_stats.top_words("the cat and the hat and the bat", n=2) == [("the", 3), ("and", 2)]


def test_reading_minutes_default_speed():
    assert text_stats.reading_minutes("word " * 200) == 1.0


def test_empty_text():
    assert text_stats.analyse("") == text_stats.TextStats(0, 0, 0, 0.0)
