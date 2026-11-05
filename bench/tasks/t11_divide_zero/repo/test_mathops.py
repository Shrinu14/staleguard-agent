import unittest
from mathops import divide

class TestDivide(unittest.TestCase):
    def test_divide(self):
        self.assertEqual(divide(6, 3), 2)

if __name__ == "__main__":
    unittest.main()
