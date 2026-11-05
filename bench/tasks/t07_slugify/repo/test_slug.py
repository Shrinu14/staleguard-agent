import unittest
from slug import slugify

class TestSlug(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(slugify("hello world"), "hello-world")

if __name__ == "__main__":
    unittest.main()
