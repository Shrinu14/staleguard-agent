import pytest
from calc import mean

def test_mean():
    assert mean([2, 4, 6]) == 4
    assert mean([5]) == 5

def test_empty():
    with pytest.raises(ValueError):
        mean([])
