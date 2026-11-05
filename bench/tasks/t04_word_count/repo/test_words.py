import unittest
from words import word_count

class TestWords(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(word_count("one two"), 2)

if __name__ == "__main__":
    unittest.main()
