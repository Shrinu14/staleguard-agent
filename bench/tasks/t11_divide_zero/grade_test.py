import pytest
from mathops import divide

def test_zero():
    with pytest.raises(ValueError, match="cannot divide by zero"):
        divide(1, 0)
    assert divide(6, 3) == 2
