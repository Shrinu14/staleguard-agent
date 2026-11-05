class Inventory:
    def __init__(self) -> None:
        self.items: dict[str, int] = {}

    def add_item(self, name: str, qty: int = 1) -> None:
        self.items[name] = self.items.get(name, 0) + qty

    def remove_item(self, name: str) -> bool:
        del self.items[name]
        return True
