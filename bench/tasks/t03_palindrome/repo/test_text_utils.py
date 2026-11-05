import unittest
from text_utils import is_palindrome

class TestPalindrome(unittest.TestCase):
    def test_simple(self):
        self.assertTrue(is_palindrome("abba"))

if __name__ == "__main__":
    unittest.main()
