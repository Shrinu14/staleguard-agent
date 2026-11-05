import unittest
from csvline import parse_line

class TestCsv(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_line("a,b"), ["a", "b"])

if __name__ == "__main__":
    unittest.main()
