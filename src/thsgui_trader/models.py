from datetime import datetime
from typing import Literal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionStatus(ApiModel):
    connected: bool
    application: str = "Tonghuashun"
    bundle_id: str
    process_id: int | None = None
    account_mode: str | None = None
    message: str | None = None
    checked_at: datetime


class AccountFunds(ApiModel):
    total_assets: float | None = None
    market_value: float | None = None
    total_profit_loss: float | None = None
    day_profit_loss: float | None = None
    cash_balance: float | None = None
    withdrawable_cash: float | None = None
    available_cash: float | None = None
    warnings: list[str] = Field(default_factory=list)
    as_of: datetime


class Position(ApiModel):
    symbol: str
    name: str | None = None
    quantity: int | None = None
    sellable_quantity: int | None = None
    cost_price: float | None = None
    latest_price: float | None = None
    floating_profit_loss: float | None = None
    market_value: float | None = None
    market: str | None = None
    shareholder_account: str | None = None


class Order(ApiModel):
    order_id: str | None = None
    symbol: str | None = None
    name: str | None = None
    side: str | None = None
    price: float | None = None
    quantity: int | None = None
    status: str | None = None
    filled_quantity: int | None = None
    order_time: str | None = None


class Trade(ApiModel):
    trade_id: str | None = None
    order_id: str | None = None
    symbol: str | None = None
    name: str | None = None
    side: str | None = None
    price: float | None = None
    quantity: int | None = None
    amount: float | None = None
    fee: float | None = None
    trade_time: str | None = None


class FundDetail(ApiModel):
    detail_id: str | None = None
    detail_time: str | None = None
    detail_type: str | None = None
    amount: float | None = None
    balance: float | None = None
    remark: str | None = None


class OrderBookLevel(ApiModel):
    side: Literal["buy", "sell"]
    level: int
    price: float | None = None
    quantity: int | None = None


class OrderBook(ApiModel):
    symbol: str
    levels: list[OrderBookLevel] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    as_of: datetime


class OrderPreviewRequest(ApiModel):
    symbol: str
    side: Literal["buy", "sell"]
    price: float
    quantity: int
    order_type: Literal["limit"] = "limit"
    board: Literal["A股", "模拟"] = "A股"


class OrderPreview(ApiModel):
    preview_id: str
    expires_at: datetime
    symbol: str
    side: Literal["buy", "sell"]
    price: float
    quantity: int
    order_type: Literal["limit"]
    account_mode: str | None = None
    available_cash: float | None = None
    sellable_quantity: int | None = None
    estimated_amount: float
    order_book: OrderBook
    warnings: list[str] = Field(default_factory=list)


class OrderSubmitRequest(ApiModel):
    preview_id: str
    confirm: bool = False


class OrderSubmitResult(ApiModel):
    accepted: bool
    verified: bool
    message: str
    preview: OrderPreview
    matching_orders: list[Order] = Field(default_factory=list)


class OrderExecuteRequest(ApiModel):
    """A single-request order intent for strategy callers.

    ``amount`` is the maximum notional to use and is converted to an A-share
    board-lot quantity at the selected opposing quote level. ``quantity`` is
    retained for callers that need an exact lot quantity.
    """

    symbol: str
    side: Literal["buy", "sell"]
    board: Literal["A股", "模拟"] = "A股"
    amount: float | None = Field(default=None, gt=0)
    quantity: int | None = Field(default=None, gt=0)
    price_level: int = Field(default=1, ge=1, le=5)
    max_price: float | None = Field(default=None, gt=0)
    min_price: float | None = Field(default=None, gt=0)
    order_type: Literal["limit"] = "limit"
    confirm: bool = False

    @model_validator(mode="after")
    def validate_intent(self) -> "OrderExecuteRequest":
        if (self.amount is None) == (self.quantity is None):
            raise ValueError("exactly one of amount or quantity is required")
        if self.quantity is not None and self.quantity % 100 != 0:
            raise ValueError("quantity must be a multiple of 100")
        if self.side == "buy" and self.min_price is not None:
            raise ValueError("min_price is only valid for sell orders")
        if self.side == "sell" and self.max_price is not None:
            raise ValueError("max_price is only valid for buy orders")
        return self


class ExecutionTimings(ApiModel):
    account_check: int
    quote_refresh: int
    form_fill: int
    confirmation: int
    order_refresh: int
    total: int


class OrderExecuteResult(OrderSubmitResult):
    price_level: int
    requested_amount: float | None = None
    timings_ms: ExecutionTimings


class CancelPreviewRequest(ApiModel):
    order_id: str
    board: Literal["A股", "模拟"] = "A股"


class CancelPreview(ApiModel):
    preview_id: str
    expires_at: datetime
    order: Order
    account_mode: str | None = None
    warnings: list[str] = Field(default_factory=list)


class CancelSubmitRequest(ApiModel):
    preview_id: str
    confirm: bool = False


class CancelTimings(ApiModel):
    selection: int
    confirmation: int
    order_refresh: int
    total: int


class CancelSubmitResult(ApiModel):
    accepted: bool
    verified: bool
    message: str
    preview: CancelPreview
    matching_orders: list[Order] = Field(default_factory=list)
    timings_ms: CancelTimings


class CancelBatchPreviewRequest(ApiModel):
    scope: Literal["buy", "sell", "all"] = "all"
    board: Literal["A股", "模拟"] = "A股"


class CancelBatchPreview(ApiModel):
    preview_id: str
    expires_at: datetime
    scope: Literal["buy", "sell", "all"]
    orders: list[Order] = Field(default_factory=list)
    account_mode: str | None = None
    warnings: list[str] = Field(default_factory=list)


class CancelBatchSubmitResult(ApiModel):
    accepted: bool
    verified: bool
    message: str
    preview: CancelBatchPreview
    matching_orders: list[Order] = Field(default_factory=list)
    timings_ms: CancelTimings


class RequestTimings(ApiModel):
    total: int


class DataResponse(ApiModel):
    data: Any
    fetched_at: datetime
    timings_ms: RequestTimings
