import unittest
from server import address

class TestServer(unittest.TestCase):
    def test_custom(self):
        self.assertEqual(address("0.0.0.0", 9000), "0.0.0.0:9000")

if __name__ == "__main__":
    unittest.main()
