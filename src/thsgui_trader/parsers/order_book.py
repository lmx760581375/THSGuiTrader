from __future__ import annotations

from datetime import datetime

from ..accessibility import AXNode
from ..errors import DataValidationError
from ..models import OrderBook, OrderBookLevel
from .common import parse_number, parse_text


def _row_values(row: AXNode) -> list[str]:
    values: list[str] = []
    for node in row.walk():
        if node is row:
            continue
        text = parse_text(node.text())
        if text and text not in values:
            values.append(text)
    return values


def parse_order_book(root: AXNode, symbol: str, now: datetime | None = None) -> OrderBook:
    tables = [node for node in root.walk() if node.role == "AXTable"]
    side_tables: list[tuple[str, AXNode]] = []
    for table in tables:
        rows = [node for node in table.children if node.role == "AXRow"]
        if len(rows) < 5:
            continue
        first_values = _row_values(rows[0])
        if "卖" in first_values:
            side_tables.append(("sell", table))
        elif "买" in first_values:
            side_tables.append(("buy", table))

    if not side_tables:
        raise DataValidationError("order book tables were not found")

    levels: list[OrderBookLevel] = []
    warnings: list[str] = []
    for side, table in side_tables:
        rows = [node for node in table.children if node.role == "AXRow"][:5]
        for index, row in enumerate(rows):
            numeric = [parse_number(value) for value in _row_values(row)]
            numeric = [value for value in numeric if value is not None]
            price = numeric[0] if numeric else None
            quantity = int(numeric[1]) if len(numeric) > 1 else None
            if price is None or quantity is None:
                warnings.append(f"{side}{5 - index if side == 'sell' else index + 1} is unavailable")
            levels.append(
                OrderBookLevel(
                    side=side,
                    level=5 - index if side == "sell" else index + 1,
                    price=price,
                    quantity=quantity,
                )
            )
    return OrderBook(symbol=symbol, levels=levels, warnings=warnings, as_of=now or datetime.now())
