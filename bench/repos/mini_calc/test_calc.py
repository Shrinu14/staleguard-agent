"""Run with: python -m unittest  (pytest also works)."""

import unittest

from calc import clamp, mean


class TestCalc(unittest.TestCase):
    def test_mean(self) -> None:
        self.assertEqual(mean([2, 4, 6]), 4)

    def test_mean_empty(self) -> None:
        with self.assertRaises(ValueError):
            mean([])

    def test_clamp(self) -> None:
        self.assertEqual(clamp(15, 0, 10), 10)
        self.assertEqual(clamp(-1, 0, 10), 0)


if __name__ == "__main__":
    unittest.main()
