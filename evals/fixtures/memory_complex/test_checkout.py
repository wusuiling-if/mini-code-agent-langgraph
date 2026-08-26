import unittest

from invoice import checkout_total
from pricing import discounted_subtotal
from shipping import shipping_fee


class CheckoutTests(unittest.TestCase):
    def test_tier_discount_is_applied(self) -> None:
        self.assertEqual(discounted_subtotal(100.0, "gold"), 85.0)
        self.assertEqual(discounted_subtotal(80.0, "silver"), 76.0)

    def test_free_shipping_uses_discounted_subtotal(self) -> None:
        self.assertEqual(shipping_fee(110.0, 93.5, expedited=False), 8.0)
        self.assertEqual(shipping_fee(120.0, 102.0, expedited=False), 0.0)

    def test_expedited_surcharge_is_never_waived(self) -> None:
        self.assertEqual(shipping_fee(120.0, 102.0, expedited=True), 12.0)
        self.assertEqual(shipping_fee(80.0, 76.0, expedited=True), 20.0)

    def test_tax_is_applied_after_shipping(self) -> None:
        self.assertEqual(
            checkout_total(110.0, "gold", 0.10, expedited=False),
            111.65,
        )
        self.assertEqual(
            checkout_total(120.0, "gold", 0.10, expedited=True),
            125.40,
        )

    def test_invalid_inputs_remain_rejected(self) -> None:
        with self.assertRaises(ValueError):
            discounted_subtotal(-1.0, "gold")
        with self.assertRaises(ValueError):
            discounted_subtotal(10.0, "unknown")


if __name__ == "__main__":
    unittest.main()
