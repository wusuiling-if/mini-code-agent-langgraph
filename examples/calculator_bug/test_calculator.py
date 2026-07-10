import unittest

from calculator import add, multiply


class CalculatorTest(unittest.TestCase):
    def test_add_positive_numbers(self):
        self.assertEqual(add(2, 3), 5)

    def test_add_negative_numbers(self):
        self.assertEqual(add(-2, -3), -5)

    def test_multiply(self):
        self.assertEqual(multiply(4, 5), 20)


if __name__ == "__main__":
    unittest.main()
