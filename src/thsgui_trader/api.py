from __future__ import annotations

from datetime import date, datetime
from threading import RLock
from time import perf_counter
from fastapi import FastAPI, HTTPException, Query

from .config import Settings
from .errors import GatewayError
from .gateways.tonghuashun import TonghuashunGuiGateway
from .models import (
    CancelBatchPreviewRequest, CancelPreviewRequest, CancelSubmitRequest,
    OrderExecuteRequest,
    OrderPreviewRequest, OrderSubmitRequest,
)


def create_app(gateway: TonghuashunGuiGateway | None = None) -> FastAPI:
    app = FastAPI(title="THSGUI Trader", version="0.1.0", docs_url="/docs")
    service = gateway or TonghuashunGuiGateway(Settings.from_env())
    gui_lock = RLock()

    def read(call):
        started = perf_counter()
        try:
            with gui_lock:
                result = call()
            return {
                "data": result,
                "fetched_at": datetime.now(),
                "timings_ms": {"total": round((perf_counter() - started) * 1000)},
            }
        except GatewayError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/health")
    def health():
        started = perf_counter()
        with gui_lock:
            status = service.get_connection_status()
        return {
            "ok": status.connected,
            "connection": status,
            "timings_ms": {"total": round((perf_counter() - started) * 1000)},
        }

    @app.get("/api/v1/connection")
    def connection():
        started = perf_counter()
        with gui_lock:
            status = service.get_connection_status()
        result = status.model_dump(mode="json")
        result["timings_ms"] = {"total": round((perf_counter() - started) * 1000)}
        return result

    @app.get("/api/v1/funds")
    def funds():
        return read(service.get_account_funds)

    @app.get("/api/v1/positions")
    def positions():
        return read(service.get_positions)

    @app.get("/api/v1/orders")
    def orders():
        return read(service.get_orders)

    @app.get("/api/v1/trades")
    def trades():
        return read(service.get_trades)

    @app.get("/api/v1/fund-details")
    def fund_details(
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
    ):
        return read(lambda: service.get_fund_details(start_date, end_date))

    @app.get("/api/v1/order-book/{symbol}")
    def order_book(symbol: str):
        return read(lambda: service.get_order_book(symbol))

    @app.post("/api/v1/orders/preview")
    def order_preview(request: OrderPreviewRequest):
        return read(lambda: service.preview_order(
            request.symbol, request.side, request.price, request.quantity, request.board
        ))

    @app.post("/api/v1/orders/submit")
    def order_submit(request: OrderSubmitRequest):
        return read(lambda: service.submit_order(request.preview_id, request.confirm))

    @app.post("/api/v1/orders/execute")
    def order_execute(request: OrderExecuteRequest):
        def execute():
            args = (
                request.symbol, request.side, request.amount, request.quantity,
                request.price_level, request.max_price, request.min_price, request.confirm,
            )
            # Keep the original gateway protocol call compatible for custom
            # adapters; the optional board is only needed for non-default use.
            if request.board == "A股":
                return service.execute_order(*args)
            return service.execute_order(*args, board=request.board)

        return read(execute)

    @app.post("/api/v1/cancel/preview")
    def cancel_preview(request: CancelPreviewRequest):
        return read(lambda: service.preview_cancel(request.order_id, request.board))

    @app.post("/api/v1/cancel/submit")
    def cancel_submit(request: CancelSubmitRequest):
        return read(lambda: service.submit_cancel(request.preview_id, request.confirm))

    @app.post("/api/v1/cancel/batch/preview")
    def cancel_batch_preview(request: CancelBatchPreviewRequest):
        return read(lambda: service.preview_cancel_batch(request.scope, request.board))

    @app.post("/api/v1/cancel/batch/submit")
    def cancel_batch_submit(request: CancelSubmitRequest):
        return read(lambda: service.submit_cancel_batch(request.preview_id, request.confirm))

    return app


app = create_app()
