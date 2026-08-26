from pricing import discounted_subtotal
from shipping import shipping_fee


def checkout_total(
    subtotal: float,
    customer_tier: str,
    tax_rate: float,
    *,
    expedited: bool = False,
) -> float:
    discounted = discounted_subtotal(subtotal, customer_tier)
    shipping = shipping_fee(
        subtotal,
        discounted,
        expedited=expedited,
    )
    # Regression: shipping is omitted from the taxable amount.
    tax = discounted * tax_rate
    return round(discounted + shipping + tax, 2)
