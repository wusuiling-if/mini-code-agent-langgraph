import unittest

from delivery import delivery_charge
from discounts import member_subtotal
from statement import settlement_total


class SettlementTests(unittest.TestCase):
    def test_membership_discount_is_applied(self) -> None:
        self.assertEqual(member_subtotal(200.0, "elite"), 160.0)
        self.assertEqual(member_subtotal(125.0, "member"), 115.0)

    def test_delivery_threshold_uses_discounted_amount(self) -> None:
        self.assertEqual(delivery_charge(180.0, 144.0, priority=False), 10.0)
        self.assertEqual(delivery_charge(200.0, 160.0, priority=False), 0.0)

    def test_priority_surcharge_is_never_waived(self) -> None:
        self.assertEqual(delivery_charge(200.0, 160.0, priority=True), 15.0)
        self.assertEqual(delivery_charge(120.0, 110.4, priority=True), 25.0)

    def test_tax_is_applied_after_delivery(self) -> None:
        self.assertEqual(
            settlement_total(180.0, "elite", 0.07, priority=False),
            164.78,
        )
        self.assertEqual(
            settlement_total(200.0, "elite", 0.07, priority=True),
            187.25,
        )

    def test_invalid_inputs_remain_rejected(self) -> None:
        with self.assertRaises(ValueError):
            member_subtotal(-1.0, "elite")
        with self.assertRaises(ValueError):
            member_subtotal(10.0, "unknown")


if __name__ == "__main__":
    unittest.main()
