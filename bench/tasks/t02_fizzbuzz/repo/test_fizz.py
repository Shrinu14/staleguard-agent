import unittest
from fizz import fizzbuzz

class TestFizz(unittest.TestCase):
    def test_fifteen(self):
        self.assertEqual(fizzbuzz(15), "FizzBuzz")

if __name__ == "__main__":
    unittest.main()
