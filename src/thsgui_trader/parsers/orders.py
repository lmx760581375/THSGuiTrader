from __future__ import annotations

import re

from ..accessibility import AXNode
from ..errors import DataValidationError
from ..models import Order
from .common import Table, find_tables, parse_number, parse_text


_FIELDS = {
    "order_id": ("\u59d4\u6258\u7f16\u53f7", "\u59d4\u6258\u53f7", "\u5408\u540c\u7f16\u53f7", "\u7533\u62a5\u7f16\u53f7"),
    "symbol": ("\u8bc1\u5238\u4ee3\u7801", "\u4ee3\u7801"),
    "name": ("\u8bc1\u5238\u540d\u79f0", "\u540d\u79f0"),
    "side": ("\u4e70\u5356\u65b9\u5411", "\u65b9\u5411", "\u64cd\u4f5c"),
    "price": ("\u59d4\u6258\u4ef7\u683c", "\u4ef7\u683c"),
    "quantity": ("\u59d4\u6258\u6570\u91cf", "\u6570\u91cf"),
    "status": ("\u59d4\u6258\u72b6\u6001", "\u72b6\u6001", "\u5907\u6ce8", "\u59d4\u6258\u5c5e\u6027"),
    "filled_quantity": ("\u5df2\u6210\u4ea4\u6570\u91cf", "\u6210\u4ea4\u6570\u91cf", "\u6210\u4ea4\u91cf"),
    "order_time": ("\u59d4\u6258\u65f6\u95f4", "\u65f6\u95f4", "\u59d4\u6258\u65e5\u671f"),
}


def _find_table(root: AXNode) -> Table | None:
    for table in find_tables(root):
        headers = set(table.headers)
        has_symbol = bool(headers & {"\u8bc1\u5238\u4ee3\u7801", "\u4ee3\u7801"})
        has_order_columns = bool(
            headers & {"\u59d4\u6258\u6570\u91cf", "\u59d4\u6258\u4ef7\u683c", "\u5408\u540c\u7f16\u53f7", "\u7533\u62a5\u7f16\u53f7", "\u59d4\u6258\u5c5e\u6027"}
        )
        if has_symbol and has_order_columns and table.rows:
            return table
    return None


def parse_orders(root: AXNode) -> list[Order]:
    table = _find_table(root)
    if table is None:
        return _parse_flat_orders(root)
    indexes = {header: i for i, header in enumerate(table.headers)}
    fields: dict[str, int] = {}
    for name, aliases in _FIELDS.items():
        for alias in aliases:
            if alias in indexes:
                fields[name] = indexes[alias]
                break

    def get(row: tuple[str, ...], field: str) -> str | None:
        index = fields.get(field)
        if index is None or index >= len(row):
            return None
        text = parse_text(row[index])
        return text or None

    def number(row: tuple[str, ...], field: str) -> float | None:
        return parse_number(get(row, field))

    return [
        Order(
            order_id=get(row, "order_id"),
            symbol=get(row, "symbol"),
            name=get(row, "name"),
            side=get(row, "side"),
            price=number(row, "price"),
            quantity=int(number(row, "quantity")) if number(row, "quantity") is not None else None,
            status=get(row, "status"),
            filled_quantity=(
                int(number(row, "filled_quantity"))
                if number(row, "filled_quantity") is not None
                else None
            ),
            order_time=get(row, "order_time"),
        )
        for row in table.rows
        if any(parse_text(cell) for cell in row)
    ]


_DATE = re.compile(r"^\d{8}$")
_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_SYMBOL = re.compile(r"^\d{6}$")


def _flat_texts(root: AXNode) -> list[str]:
    return [
        parse_text(node.text())
        for node in root.walk()
        if node.role in {"AXStaticText", "AXText", "AXLabel"} and parse_text(node.text())
    ]


def _parse_flat_orders(root: AXNode) -> list[Order]:
    """Parse the current macOS client, which exposes rows as flat text nodes."""
    values = _flat_texts(root)
    orders: list[Order] = []
    index = 0
    while index + 10 < len(values):
        if not (_DATE.fullmatch(values[index]) and _TIME.fullmatch(values[index + 1])):
            index += 1
            continue
        if not _SYMBOL.fullmatch(values[index + 2]):
            index += 1
            continue
        fields = values[index:index + 11]
        quantity = parse_number(fields[6])
        filled_quantity = parse_number(fields[7])
        price = parse_number(fields[8])
        if quantity is None or price is None:
            index += 1
            continue
        orders.append(
            Order(
                order_id=fields[10],
                symbol=fields[2],
                name=fields[3],
                side=fields[4],
                price=price,
                quantity=int(quantity),
                status=fields[5],
                filled_quantity=int(filled_quantity) if filled_quantity is not None else None,
                order_time=f"{fields[0]} {fields[1]}",
            )
        )
        index += 11
    return orders
