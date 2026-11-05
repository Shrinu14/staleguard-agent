import unittest
from inventory import Inventory

class TestInventory(unittest.TestCase):
    def test_add(self):
        inv = Inventory()
        inv.add_item("apple", 2)
        self.assertEqual(inv.items["apple"], 2)

if __name__ == "__main__":
    unittest.main()
