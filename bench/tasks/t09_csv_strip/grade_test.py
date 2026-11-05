from csvline import parse_line

def test_strip():
    assert parse_line("a, b ,c") == ["a", "b", "c"]
    assert parse_line("x") == ["x"]
