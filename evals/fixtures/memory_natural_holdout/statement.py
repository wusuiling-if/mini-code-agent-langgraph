from delivery import delivery_charge
from discounts import member_subtotal


def settlement_total(
    amount: float,
    membership: str,
    tax_rate: float,
    *,
    priority: bool = False,
) -> float:
    discounted = member_subtotal(amount, membership)
    delivery = delivery_charge(amount, discounted, priority=priority)
    # Regression: delivery is omitted from the taxable amount.
    tax = discounted * tax_rate
    return round(discounted + delivery + tax, 2)
