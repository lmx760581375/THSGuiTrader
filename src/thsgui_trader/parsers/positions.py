from __future__ import annotations

from ..accessibility import AXNode
from ..errors import DataValidationError
from ..models import Position
from .common import Table, find_tables, parse_number, parse_text


_ALIASES = {
    "symbol": {"\u8bc1\u5238\u4ee3\u7801", "\u4ee3\u7801"},
    "name": {"\u8bc1\u5238\u540d\u79f0", "\u540d\u79f0"},
    "latest_price": {"\u5e02\u4ef7", "\u6700\u65b0\u4ef7"},
    "floating_profit_loss": {"\u76c8\u4e8f", "\u6d6e\u52a8\u76c8\u4e8f"},
    "quantity": {"\u5b9e\u9645\u6570\u91cf", "\u6301\u4ed3\u6570\u91cf"},
    "sellable_quantity": {"\u53ef\u7528\u4f59\u989d", "\u53ef\u5356\u6570\u91cf", "\u80a1\u7968\u4f59\u989d"},
    "cost_price": {"\u6210\u672c\u4ef7"},
    "market_value": {"\u5e02\u503c"},
    "market": {"\u4ea4\u6613\u5e02\u573a"},
    "shareholder_account": {"\u80a1\u4e1c\u8d26\u6237"},
}


def _position_table(root: AXNode) -> Table:
    required = {"\u8bc1\u5238\u4ee3\u7801", "\u8bc1\u5238\u540d\u79f0"}
    candidates = [table for table in find_tables(root) if required.issubset(set(table.headers))]
    if candidates:
        return candidates[0]
    # Tonghuashun's current macOS build exposes the position table data but
    # renders its AXSortButton headers in a sibling container. Keep a narrow
    # positional fallback for this stable 15-column table layout.
    for table in find_tables(root):
        if table.rows and len(table.rows[0]) >= 15:
            return Table(
                headers=(
                    "证券代码", "证券名称", "市价", "盈亏", "当日盈亏",
                    "浮动盈亏比(%)", "实际数量", "股票余额", "可用余额", "冻结数量",
                    "成本价", "市值", "仓位占比(%)", "交易市场", "股东账户",
                ),
                rows=table.rows,
            )
    raise DataValidationError("positions table was not found")


def parse_positions(root: AXNode) -> list[Position]:
    table = _position_table(root)
    index = {header: position for position, header in enumerate(table.headers)}
    field_indexes: dict[str, int] = {}
    for field, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in index:
                field_indexes[field] = index[alias]
                break
    symbol_index = field_indexes.get("symbol")
    if symbol_index is None:
        raise DataValidationError("positions table has no symbol column")
    positions: list[Position] = []
    for row in table.rows:
        if symbol_index >= len(row):
            continue
        symbol = parse_text(row[symbol_index])
        if not symbol or not any(char.isdigit() for char in symbol):
            continue

        def number(field: str) -> float | None:
            position = field_indexes.get(field)
            return parse_number(row[position]) if position is not None and position < len(row) else None

        def text(field: str) -> str | None:
            position = field_indexes.get(field)
            value = parse_text(row[position]) if position is not None and position < len(row) else ""
            return value or None

        quantity = number("quantity")
        sellable = number("sellable_quantity")
        positions.append(
            Position(
                symbol=symbol,
                name=text("name"),
                quantity=int(quantity) if quantity is not None else None,
                sellable_quantity=int(sellable) if sellable is not None else None,
                cost_price=number("cost_price"),
                latest_price=number("latest_price"),
                floating_profit_loss=number("floating_profit_loss"),
                market_value=number("market_value"),
                market=text("market"),
                shareholder_account=text("shareholder_account"),
            )
        )
    return positions
