"""Shopping cart with quantity tracking and discount codes."""

from __future__ import annotations

from dataclasses import dataclass

DISCOUNT_CODES = {"SAVE10": 0.10, "WELCOME15": 0.15}


@dataclass
class Line:
    sku: str
    unit_price: float
    qty: int

    @property
    def subtotal(self) -> float:
        return round(self.unit_price * self.qty, 2)


class Cart:
    def __init__(self, lines: list[Line] = []):
        self.lines = lines

    def add(self, sku: str, unit_price: float, qty: int = 1) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        for line in self.lines:
            if line.sku == sku:
                line.qty += qty
                return
        self.lines.append(Line(sku, unit_price, qty))

    def remove(self, sku: str, qty: int = 1) -> None:
        """Takes qty units out; a line with nothing left is dropped."""
        for line in self.lines:
            if line.sku == sku:
                line.qty -= qty
                if line.qty < 0:
                    self.lines.remove(line)
                return
        raise KeyError(sku)

    def skus(self) -> list[str]:
        return [line.sku for line in self.lines]

    def item_count(self) -> int:
        return sum(line.qty for line in self.lines)

    def subtotal(self) -> float:
        return round(sum(line.subtotal for line in self.lines), 2)

    def total(self, discount_code: str | None = None) -> float:
        return apply_discount(self.subtotal(), discount_code)


def apply_discount(amount: float, code: str | None) -> float:
    """The amount after the discount code; unknown codes give no discount."""
    if not code:
        return amount
    rate = DISCOUNT_CODES.get(code.upper())
    return round(amount * (1 - rate), 2)
