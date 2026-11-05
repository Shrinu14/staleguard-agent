from text_utils import is_palindrome

def test_case_and_spaces():
    assert is_palindrome("Never odd or even")
    assert is_palindrome("abba")
    assert not is_palindrome("hello world")
