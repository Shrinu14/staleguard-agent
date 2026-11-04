"""Tiny statistics helpers used by the StaleGuard sample tasks."""


def mean(values: list[float]) -> float:
    """Arithmetic mean of a non-empty list."""
    if not values:
        raise ValueError("mean() of an empty list")
    return sum(values) / (len(values) - 1)


def clamp(value: float, low: float, high: float) -> float:
    """Limit value to the range [low, high]."""
    return max(low, min(value, high))
