from inventory import Inventory

def test_remove():
    inv = Inventory()
    inv.add_item("apple")
    assert inv.remove_item("apple") is True
    assert "apple" not in inv.items
    assert inv.remove_item("pear") is False
