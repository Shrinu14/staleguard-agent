import unittest
from convert import c_to_f

class TestConvert(unittest.TestCase):
    def test_freezing(self):
        self.assertEqual(c_to_f(0), 32)

if __name__ == "__main__":
    unittest.main()
