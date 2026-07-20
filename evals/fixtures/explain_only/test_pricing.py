import unittest

from pricing import apply_discount


class PricingTests(unittest.TestCase):
    def test_applies_fractional_discount(self) -> None:
        self.assertEqual(apply_discount(100.0, 0.2), 80.0)


if __name__ == "__main__":
    unittest.main()
