from datetime import datetime

from fastapi.testclient import TestClient

from thsgui_trader.api import create_app
from thsgui_trader.models import (
    AccountFunds, ConnectionStatus, ExecutionTimings, OrderBook, OrderExecuteResult,
    OrderPreview,
)


class FakeGateway:
    last_board = None

    def get_connection_status(self):
        return ConnectionStatus(
            connected=True,
            bundle_id="test",
            process_id=1,
            account_mode="模拟",
            checked_at=datetime(2026, 1, 1),
        )

    def get_account_funds(self):
        return AccountFunds(total_assets=100, as_of=datetime(2026, 1, 1))

    def get_positions(self):
        return []

    def get_orders(self):
        return []

    def get_trades(self):
        return []

    def get_fund_details(self, start_date, end_date):
        return []

    def execute_order(
        self, symbol, side, amount, quantity, price_level, max_price, min_price, confirm,
        board="A股",
    ):
        assert confirm is True
        if side == "buy":
            assert (amount, quantity, price_level) == (1000.0, None, 2)
        else:
            assert (amount, quantity, price_level) == (None, 100, 2)
        self.last_board = board
        preview = OrderPreview(
            preview_id="internal",
            expires_at=datetime(2026, 1, 1),
            symbol=symbol,
            side=side,
            price=10.0,
            quantity=quantity or 100,
            order_type="limit",
            estimated_amount=1000.0,
            order_book=OrderBook(symbol=symbol, as_of=datetime(2026, 1, 1)),
        )
        return OrderExecuteResult(
            accepted=True,
            verified=True,
            message="submitted",
            preview=preview,
            price_level=price_level,
            requested_amount=amount,
            timings_ms=ExecutionTimings(
                account_check=1,
                quote_refresh=2,
                form_fill=3,
                confirmation=4,
                order_refresh=5,
                total=15,
            ),
        )


def test_read_only_api():
    client = TestClient(create_app(FakeGateway()))
    assert client.get("/health").json()["ok"] is True
    response = client.get("/api/v1/funds")
    assert response.status_code == 200
    assert response.json()["data"]["total_assets"] == 100.0


def test_execute_api_passes_single_order_intent():
    client = TestClient(create_app(FakeGateway()))
    response = client.post("/api/v1/orders/execute", json={
        "symbol": "000001",
        "side": "buy",
        "amount": 1000,
        "price_level": 2,
        "confirm": True,
    })
    assert response.status_code == 200
    assert response.json()["data"]["timings_ms"]["total"] == 15


def test_execute_api_rejects_ambiguous_size():
    client = TestClient(create_app(FakeGateway()))
    response = client.post("/api/v1/orders/execute", json={
        "symbol": "000001",
        "side": "buy",
        "amount": 1000,
        "quantity": 100,
        "confirm": True,
    })
    assert response.status_code == 422


def test_execute_api_passes_simulated_board_to_gateway():
    gateway = FakeGateway()
    client = TestClient(create_app(gateway))
    response = client.post("/api/v1/orders/execute", json={
        "symbol": "000001",
        "side": "sell",
        "board": "模拟",
        "quantity": 100,
        "price_level": 2,
        "confirm": True,
    })
    assert response.status_code == 200
    assert gateway.last_board == "模拟"
