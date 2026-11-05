import pathlib
import pricing
from cart import checkout

def test_renamed():
    assert hasattr(pricing, "total_price")
    assert not hasattr(pricing, "calc_total")
    assert pricing.total_price([10, 20], tax=0.1) == 33.0
    assert checkout([10, 20]) == 33.0
    assert "calc_total" not in pathlib.Path("cart.py").read_text()
