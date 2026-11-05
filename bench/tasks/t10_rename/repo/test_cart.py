import unittest
from cart import checkout

class TestCart(unittest.TestCase):
    def test_checkout(self):
        self.assertEqual(checkout([10, 20]), 33.0)

if __name__ == "__main__":
    unittest.main()
