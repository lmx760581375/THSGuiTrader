from __future__ import annotations

from ..accessibility import AXNode
from ..errors import DataValidationError
from ..models import FundDetail, Trade
from .common import Table, find_tables, parse_number, parse_text


_TRADE_FIELDS = {
    "trade_id": {"\u6210\u4ea4\u7f16\u53f7", "\u6210\u4ea4\u53f7"},
    "order_id": {"\u59d4\u6258\u7f16\u53f7", "\u59d4\u6258\u53f7"},
    "symbol": {"\u8bc1\u5238\u4ee3\u7801", "\u4ee3\u7801"},
    "name": {"\u8bc1\u5238\u540d\u79f0", "\u540d\u79f0"},
    "side": {"\u4e70\u5356\u65b9\u5411", "\u65b9\u5411"},
    "price": {"\u6210\u4ea4\u4ef7\u683c", "\u6210\u4ea4\u4ef7"},
    "quantity": {"\u6210\u4ea4\u6570\u91cf", "\u6570\u91cf"},
    "amount": {"\u6210\u4ea4\u91d1\u989d", "\u6210\u4ea4\u989d"},
    "fee": {"\u624b\u7eed\u8d39", "\u8d39\u7528"},
    "trade_time": {"\u6210\u4ea4\u65f6\u95f4", "\u65f6\u95f4"},
}


def _table(root: AXNode, required: set[str], marker: str) -> Table | None:
    for table in find_tables(root):
        headers = set(table.headers)
        if required.intersection(headers) and marker in headers and table.rows:
            return table
    return None


def _parse_table(table: Table, model_cls, fields: dict[str, set[str]]):
    indexes = {header: i for i, header in enumerate(table.headers)}
    found = {}
    for field, aliases in fields.items():
        for alias in aliases:
            if alias in indexes:
                found[field] = indexes[alias]
                break

    def get(row: tuple[str, ...], field: str) -> str | None:
        index = found.get(field)
        if index is None or index >= len(row):
            return None
        text = parse_text(row[index])
        return text or None

    numeric = {"price", "quantity", "amount", "fee"}
    result = []
    for row in table.rows:
        if not any(parse_text(cell) for cell in row):
            continue
        values = {field: get(row, field) for field in fields}
        for field in numeric:
            if field in values:
                value = parse_number(values[field])
                values[field] = int(value) if field == "quantity" and value is not None else value
        result.append(model_cls(**values))
    return result


def parse_trades(root: AXNode) -> list[Trade]:
    table = _table(root, {"\u6210\u4ea4\u7f16\u53f7", "\u6210\u4ea4\u4ef7\u683c"}, "\u6210\u4ea4\u65f6\u95f4")
    if table is None:
        return []
    return _parse_table(table, Trade, _TRADE_FIELDS)


def parse_fund_details(root: AXNode) -> list[FundDetail]:
    fields = {
        "detail_id": {"\u5e8f\u53f7", "\u660e\u7ec6\u7f16\u53f7"},
        "detail_time": {"\u6210\u4ea4\u65f6\u95f4", "\u65f6\u95f4", "\u53d1\u751f\u65e5\u671f"},
        "detail_type": {"\u7c7b\u578b", "\u4e1a\u52a1\u7c7b\u578b"},
        "amount": {"\u53d1\u751f\u91d1\u989d", "\u91d1\u989d"},
        "balance": {"\u4f59\u989d", "\u8d44\u91d1\u4f59\u989d"},
        "remark": {"\u5907\u6ce8", "\u8bf4\u660e"},
    }
    table = _table(root, {"\u91d1\u989d", "\u53d1\u751f\u91d1\u989d"}, "\u4f59\u989d")
    if table is None:
        return []
    return _parse_table(table, FundDetail, fields)
