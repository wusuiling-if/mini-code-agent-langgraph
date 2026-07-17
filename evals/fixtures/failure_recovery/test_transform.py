import unittest

from transform import triple


class TransformTests(unittest.TestCase):
    def test_triples_positive_and_zero_values(self) -> None:
        self.assertEqual(triple(4), 12)
        self.assertEqual(triple(0), 0)


if __name__ == "__main__":
    unittest.main()
