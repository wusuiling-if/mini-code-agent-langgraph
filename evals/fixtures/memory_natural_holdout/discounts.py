MEMBER_DISCOUNTS = {
    "regular": 0.0,
    "member": 0.08,
    "elite": 0.20,
}


def member_subtotal(amount: float, membership: str) -> float:
    if amount < 0:
        raise ValueError("amount must be non-negative")
    if membership not in MEMBER_DISCOUNTS:
        raise ValueError("unknown membership")
    # Regression: the configured membership discount is ignored.
    return amount
