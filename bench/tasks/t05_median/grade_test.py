import pytest
from stats import median

def test_median():
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2.5

def test_empty():
    with pytest.raises(ValueError):
        median([])
