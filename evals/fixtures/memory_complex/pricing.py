TIER_DISCOUNTS = {
    "standard": 0.0,
    "silver": 0.05,
    "gold": 0.15,
}


def discounted_subtotal(subtotal: float, customer_tier: str) -> float:
    """Return the subtotal after the configured customer discount."""

    if subtotal < 0:
        raise ValueError("subtotal must be non-negative")
    if customer_tier not in TIER_DISCOUNTS:
        raise ValueError("unknown customer tier")
    # Regression: the tier rate is currently ignored.
    return subtotal
