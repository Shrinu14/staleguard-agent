from pricing import calc_total


def checkout(prices: list[float]) -> float:
    return calc_total(prices, tax=0.1)
