import unittest

from invoice import invoice_total


class InvoiceTests(unittest.TestCase):
    def test_discount_and_shipping(self):
        self.assertEqual(invoice_total(100.0, 0.2, 5.0), 85.0)
