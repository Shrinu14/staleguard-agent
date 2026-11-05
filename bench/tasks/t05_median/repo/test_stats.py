import unittest
from stats import mean

class TestStats(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(mean([1, 2, 3]), 2)

if __name__ == "__main__":
    unittest.main()
