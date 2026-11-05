from words import word_count

def test_whitespace():
    assert word_count("a  b\nc") == 3
    assert word_count("  lead and trail  ") == 3
    assert word_count("") == 0
