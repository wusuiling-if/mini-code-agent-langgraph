def apply_discount(total: float, rate: float) -> float:
    """Return the amount left after applying a fractional discount rate."""
    return total * (1 - rate)
