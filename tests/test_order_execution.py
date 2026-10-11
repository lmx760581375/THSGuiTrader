from datetime import datetime

import pytest

from thsgui_trader.accessibility import AXNode
from thsgui_trader.errors import DataValidationError
from thsgui_trader.gateways.tonghuashun import TonghuashunGuiGateway
from thsgui_trader.models import (
    Order, OrderBook, OrderBookLevel, OrderExecuteRequest, OrderPreview,
)


def test_cancel_rebuilds_order_table_before_selecting_rows():
    gateway = TonghuashunGuiGateway.__new__(TonghuashunGuiGateway)
    calls = []

    class _Root:
        def release(self):
            pass

    def select_tab(label):
        calls.append(label)
        return _Root()

    gateway._select_tab = select_tab
    gateway._fresh_orders_root()
    assert calls == ["持仓", "委托"]


def test_cancel_selection_resets_table_level_selection():
    gateway = TonghuashunGuiGateway.__new__(TonghuashunGuiGateway)
    table = AXNode(
        role="AXTable",
        children=[
            AXNode(role="AXRow", children=[AXNode(role="AXStaticText", value="100")]),
            AXNode(role="AXRow", children=[AXNode(role="AXStaticText", value="101")]),
        ],
    )
    root = AXNode(
        role="AXApplication",
        children=[table, AXNode(role="AXButton", title="撤买")],
    )

    class _SelectionClient:
        def __init__(self):
            self.count = 0
            self.cleared = False

        def clear_selected_rows(self, node):
            self.count = 0
            self.cleared = True

        def set_selected(self, node, value):
            pass

        def select(self, node):
            self.count = 1

        def press(self, node):
            pass

    gateway.client = _SelectionClient()
    gateway._select_cancel_rows(root, ["100"], "buy")
    assert gateway.client.cleared is True
    assert gateway.client.count == 1


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


def test_order_book_complete_rejects_empty_refresh_rows():
    incomplete = OrderBook(
        symbol="000001",
        as_of=datetime(2026, 1, 1),
        levels=[
            OrderBookLevel(side="sell", level=5 - index, price=None, quantity=None)
            for index in range(5)
        ] + [
            OrderBookLevel(side="buy", level=index + 1, price=11.5, quantity=100)
            for index in range(5)
        ],
    )
    complete = OrderBook(
        symbol="000001",
        as_of=datetime(2026, 1, 1),
        levels=[
            OrderBookLevel(side="sell", level=5 - index, price=11.6, quantity=100)
            for index in range(5)
        ] + [
            OrderBookLevel(side="buy", level=index + 1, price=11.5, quantity=100)
            for index in range(5)
        ],
    )
    assert TonghuashunGuiGateway._order_book_complete(incomplete) is False
    assert TonghuashunGuiGateway._order_book_complete(complete) is True


def _trade_form(symbol: str, name: str) -> AXNode:
    return AXNode(
        role="AXApplication",
        children=[
            AXNode(role="AXStaticText", value="代码"),
            AXNode(role="AXStaticText", value=name),
            AXNode(role="AXStaticText", value="价格"),
            AXNode(role="AXStaticText", value="数量"),
            AXNode(role="AXTextField", value="1.000"),
            AXNode(role="AXTextField", value=symbol),
            AXNode(role="AXTextField", value=""),
        ],
    )


def test_quote_identity_rejects_complete_but_stale_previous_security():
    stale = _trade_form("588170", "科创50ETF工银")
    current = _trade_form("588170", "科创半导体ETF华夏")

    assert not TonghuashunGuiGateway._quote_identity_matches(
        stale, "588170", "588050", "科创50ETF工银"
    )
    assert TonghuashunGuiGateway._quote_identity_matches(
        current, "588170", "588050", "科创50ETF工银"
    )


def test_empty_security_name_does_not_use_price_label():
    root = AXNode(
        role="AXApplication",
        children=[
            AXNode(role="AXStaticText", value="代码"),
            AXNode(role="AXStaticText", value="价格"),
        ],
    )
    assert TonghuashunGuiGateway._displayed_security_name(root) == ""


def test_trade_side_switches_before_form_input():
    gateway = TonghuashunGuiGateway.__new__(TonghuashunGuiGateway)
    sell_form = AXNode(
        role="AXApplication",
        children=[
            AXNode(role="AXButton", title="买入"),
            AXNode(role="AXButton", title="确定卖出"),
        ],
    )
    buy_form = AXNode(
        role="AXApplication",
        children=[AXNode(role="AXButton", title="确定买入")],
    )
    snapshots = iter((sell_form, buy_form))
    pressed = []
    gateway._snapshot = lambda: next(snapshots)
    gateway.client = type("Client", (), {"press": lambda self, node: pressed.append(node.text())})()

    gateway._select_trade_side("buy")
    assert pressed == ["买入"]


def test_code_field_clears_then_types_and_commits():
    gateway = TonghuashunGuiGateway.__new__(TonghuashunGuiGateway)
    field = AXNode(role="AXTextField", value="")
    calls = []

    class _FieldClient:
        value = ""

        def set_value(self, node, value):
            self.value = value
            calls.append(("set_value", value))

        def read_attribute(self, node, name):
            return self.value if name == "AXValue" else None

        def type_text(self, node, value):
            self.value = value
            calls.append(("type_text", value))

        def action_names(self, node):
            return ["AXConfirm"]

        def perform_action(self, node, action):
            calls.append(action)

        def press_tab(self):
            calls.append("tab")

    gateway.client = _FieldClient()
    gateway._write_live_field(field, "代码", "588170")
    assert calls == [
        ("set_value", ""),
        ("type_text", "588170"),
        "AXConfirm",
        "tab",
    ]


def test_unfilled_order_is_cancellable():
    assert TonghuashunGuiGateway._is_cancellable(Order(status="未成交")) is True
    assert TonghuashunGuiGateway._is_cancellable(Order(status="已成交")) is False


def test_cancel_scope_matches_chinese_and_english_sides():
    assert TonghuashunGuiGateway._order_side_matches(Order(side="买入"), "buy")
    assert TonghuashunGuiGateway._order_side_matches(Order(side="sell"), "sell")
    assert not TonghuashunGuiGateway._order_side_matches(Order(side="买入"), "sell")


def test_latest_matching_order_does_not_return_historical_duplicates():
    matches = [
        Order(order_id="62", symbol="588170", price=0.868, quantity=100,
              order_time="20261011 19:37:30"),
        Order(order_id="64", symbol="588170", price=0.868, quantity=100,
              order_time="20261011 19:38:00"),
    ]
    latest = TonghuashunGuiGateway._latest_matching_order(matches)
    assert [order.order_id for order in latest] == ["64"]


def test_submitted_order_match_requires_recent_time_and_direction():
    submitted_after = datetime(2026, 10, 11, 22, 30, 0)
    preview = OrderPreview(
        preview_id="preview",
        expires_at=submitted_after,
        symbol="588170",
        side="buy",
        price=0.868,
        quantity=100,
        order_type="limit",
        estimated_amount=86.8,
        order_book=OrderBook(symbol="588170", as_of=submitted_after),
    )
    orders = [
        Order(order_id="1", symbol="588170", side="买入", price=0.868,
              quantity=100, order_time="20261011 22:20:00"),
        Order(order_id="2", symbol="588170", side="卖出", price=0.868,
              quantity=100, order_time="20261011 22:30:01"),
        Order(order_id="3", symbol="588170", side="买入", price=0.868,
              quantity=100, order_time="20261011 22:30:02"),
    ]

    matches = TonghuashunGuiGateway._matching_submitted_orders(
        orders, preview, submitted_after
    )
    assert [order.order_id for order in matches] == ["3"]


def test_execute_request_rejects_invalid_price_guard_for_side():
    with pytest.raises(ValueError, match="max_price"):
        OrderExecuteRequest(
            symbol="000001", side="sell", quantity=100, max_price=11.0
        )
    with pytest.raises(ValueError, match="min_price"):
        OrderExecuteRequest(
            symbol="000001", side="buy", quantity=100, min_price=11.0
        )


def test_execute_request_requires_board_lot_quantity():
    with pytest.raises(ValueError):
        OrderExecuteRequest(symbol="000001", side="buy", quantity=1)
