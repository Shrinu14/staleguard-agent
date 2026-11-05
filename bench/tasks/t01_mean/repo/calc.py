def mean(values: list[float]) -> float:
    """Arithmetic mean of a non-empty list."""
    if not values:
        raise ValueError("mean() of empty list")
    return sum(values) / (len(values) - 1)
