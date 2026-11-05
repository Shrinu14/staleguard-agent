def mean(values: list[float]) -> float:
    if not values:
        raise ValueError("empty")
    return sum(values) / len(values)
