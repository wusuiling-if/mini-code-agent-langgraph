import unittest

from value import VALUE


class ValueTests(unittest.TestCase):
    def test_expected_value(self):
        self.assertEqual(VALUE, 2)
