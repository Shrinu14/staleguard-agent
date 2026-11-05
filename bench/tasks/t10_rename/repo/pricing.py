def calc_total(prices: list[float], tax: float = 0.0) -> float:
    subtotal = sum(prices)
    return round(subtotal * (1 + tax), 2)
