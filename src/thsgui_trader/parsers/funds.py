from __future__ import annotations

from datetime import datetime

from ..errors import DataValidationError
from ..models import AccountFunds
from ..accessibility import AXNode
from .common import find_tables, parse_number, parse_text


_FIELDS = {
    "\u603b\u8d44\u4ea7": "total_assets",
    "\u603b\u5e02\u503c": "market_value",
    "\u603b\u76c8\u4e8f": "total_profit_loss",
    "\u5f53\u65e5\u76c8\u4e8f": "day_profit_loss",
    "\u8d44\u91d1\u4f59\u989d": "cash_balance",
    "\u53ef\u53d6\u91d1\u989d": "withdrawable_cash",
    "\u53ef\u7528\u91d1\u989d": "available_cash",
}


def parse_funds(root: AXNode, now: datetime | None = None) -> AccountFunds:
    values: dict[str, float | None] = {}
    for table in find_tables(root):
        for row in table.rows:
            if len(row) < 2:
                continue
            field = parse_text(row[0])
            key = _FIELDS.get(field)
            if key:
                values[key] = parse_number(row[1])
    if not values:
        raise DataValidationError("funds table was not found")
    warnings: list[str] = []
    total_assets = values.get("total_assets")
    market_value = values.get("market_value")
    cash_balance = values.get("cash_balance")
    if total_assets is not None and market_value is not None and cash_balance is not None:
        if abs(total_assets - market_value - cash_balance) > max(1.0, total_assets * 0.01):
            warnings.append("total assets, market value and cash balance differ materially")
    return AccountFunds(warnings=warnings, as_of=now or datetime.now(), **values)
