from datetime import datetime

import pytest

from thsgui_trader.errors import DataValidationError
from thsgui_trader.gateways.tonghuashun import TonghuashunGuiGateway
from thsgui_trader.models import Order, OrderBook, OrderBookLevel


def test_amount_quantity_uses_complete_board_lots_only():
    assert TonghuashunGuiGateway._amount_quantity(1000, 11.6) == 0
    assert TonghuashunGuiGateway._amount_quantity(1160, 11.6) == 100
    assert TonghuashunGuiGateway._amount_quantity(2350, 11.6) == 200


def test_opposing_level_uses_ask_for_buy_and_bid_for_sell():
    book = OrderBook(
        symbol="000001",
        as_of=datetime(2026, 1, 1),
        levels=[
            OrderBookLevel(side="sell", level=2, price=11.61, quantity=100),
            OrderBookLevel(side="buy", level=2, price=11.58, quantity=100),
        ],
    )
    assert TonghuashunGuiGateway._opposing_level(book, "buy", 2).price == 11.61
    assert TonghuashunGuiGateway._opposing_level(book, "sell", 2).price == 11.58


def test_opposing_level_rejects_missing_or_invalid_quote():
    book = OrderBook(symbol="000001", as_of=datetime(2026, 1, 1))
    with pytest.raises(DataValidationError, match="opposing quote is unavailable"):
        TonghuashunGuiGateway._opposing_level(book, "buy", 5)


def test_unfilled_order_is_cancellable():
    assert TonghuashunGuiGateway._is_cancellable(Order(status="未成交")) is True
    assert TonghuashunGuiGateway._is_cancellable(Order(status="已成交")) is False
