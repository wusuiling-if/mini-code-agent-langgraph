from pricing import apply_discount


def invoice_total(subtotal: float, rate: float, shipping: float) -> float:
    return apply_discount(subtotal, rate)
