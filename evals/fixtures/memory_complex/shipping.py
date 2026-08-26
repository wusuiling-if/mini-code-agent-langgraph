def shipping_fee(
    original_subtotal: float,
    discounted_subtotal: float,
    *,
    expedited: bool,
) -> float:
    """Calculate shipping without changing the established public signature."""

    # Regression: eligibility is checked before discounts, and expedited
    # shipping is incorrectly waived together with standard shipping.
    if original_subtotal >= 100:
        return 0.0
    return 20.0 if expedited else 8.0
