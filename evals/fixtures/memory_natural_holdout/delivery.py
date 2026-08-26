def delivery_charge(
    original_amount: float,
    member_amount: float,
    *,
    priority: bool,
) -> float:
    """Return the delivery charge while preserving the public signature."""

    # Regression: the threshold is checked before the membership discount,
    # and priority delivery is waived above that threshold.
    if original_amount >= 150:
        return 0.0
    return 25.0 if priority else 10.0
