from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from datetime import date, datetime, timedelta
import re
import time
from time import perf_counter
from uuid import uuid4

from ..accessibility import AXNode, MacAccessibilityClient
from ..config import Settings
from ..errors import AccessibilityUnavailable, DataValidationError, GatewayError
from ..models import (
    AccountFunds, CancelBatchPreview, CancelBatchSubmitResult, CancelPreview,
    CancelSubmitResult, CancelTimings, ConnectionStatus, FundDetail,
    ExecutionTimings, Order, OrderBook, OrderExecuteResult, OrderPreview,
    OrderSubmitResult, Position, Trade,
)
from ..parsers import (
    parse_fund_details, parse_funds, parse_order_book, parse_orders,
    parse_positions, parse_trades,
)

class TonghuashunGuiGateway:
    """Tonghuashun adapter backed by macOS Accessibility.

    Query methods are always available. Order and cancel actions require both
    an explicit preview confirmation and the corresponding settings flag.
    """

    _TABS = ("\u6301\u4ed3", "\u59d4\u6258", "\u6210\u4ea4", "\u8d44\u91d1\u660e\u7ec6")
    _BOARDS = ("A\u80a1", "\u6a21\u62df")
    # The board switch is asynchronous. 0.45s is the shortest stable value
    # observed across A股 and 模拟; the following form snapshot remains the
    # authoritative readiness check.
    _BOARD_SETTLE_SECONDS = 0.45
    _TAB_SETTLE_SECONDS = 0.12

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.client = MacAccessibilityClient(
            self.settings.bundle_id,
            max_nodes=self.settings.max_tree_nodes,
        )
        self._order_previews: dict[str, OrderPreview] = {}
        self._cancel_previews: dict[str, CancelPreview] = {}
        self._cancel_batch_previews: dict[str, CancelBatchPreview] = {}
        # The client does not expose a reliable selected-state attribute for
        # the board buttons, so keep the last board selected by this gateway.
        self._board: str | None = None
        # Identity metadata is used only to reject a stale quote after a
        # failed transition. No quote prices or quantities are cached.
        self._last_quote_identity: tuple[str, str] | None = None

    def _snapshot(self) -> AXNode:
        return self.client.snapshot()

    @staticmethod
    def _find_button(root: AXNode, label: str) -> AXNode | None:
        candidates = [
            node
            for node in root.walk()
            if node.role == "AXButton" and node.enabled and node.text() == label
        ]
        return candidates[0] if candidates else None

    def _select_tab(self, label: str) -> AXNode:
        root = self._snapshot()
        try:
            button = self._find_button(root, label)
            if button is not None:
                self.client.press(button)
            else:
                root.release()
                root = self.client.snapshot(prefer_focused=False)
                button = self._find_button(root, label)
                if button is None:
                    raise DataValidationError(f"tab is not available: {label}")
                self.client.press(button)
        finally:
            root.release()
        time.sleep(self._TAB_SETTLE_SECONDS)
        return self._snapshot()

    def _select_board(self, board: str = "A\u80a1", *, force: bool = True) -> None:
        """Select the board used for quote reads and order operations."""
        if board not in self._BOARDS:
            raise DataValidationError(f"unsupported board: {board}")
        if not force and self._board == board:
            return
        root = self._snapshot()
        try:
            button = self._find_button(root, board)
            if button is None:
                root.release()
                root = self.client.snapshot(prefer_focused=False)
                button = self._find_button(root, board)
            if button is None:
                raise DataValidationError(f"board is not available: {board}")
            self.client.press(button)
        finally:
            root.release()
        time.sleep(self._BOARD_SETTLE_SECONDS)
        self._board = board

    @staticmethod
    def _text_fields(root: AXNode) -> list[AXNode]:
        return [node for node in root.walk() if node.role == "AXTextField" and node.enabled]

    @staticmethod
    def _form_field(root: AXNode, label: str) -> AXNode | None:
        """Locate the trade form field in its stable visual order.

        The current macOS client exposes the fields without labels and orders
        them as price, code, quantity. The global tree also contains the
        toolbar search field, which must be excluded explicitly.
        """
        nodes = list(root.walk())
        fields = [
            node
            for node in nodes
            if node.role == "AXTextField"
            and node.enabled
            and node.text().strip() != "搜索"
        ]
        labels = [
            index
            for index, node in enumerate(nodes)
            if node.role in {"AXStaticText", "AXText", "AXLabel"}
            and node.text().strip() == label
        ]
        if not fields or not labels:
            return None
        if label == "\u4ee3\u7801":
            return fields[1] if len(fields) >= 2 else None
        if label == "\u6570\u91cf":
            return fields[2] if len(fields) >= 3 else None
        if label == "\u4ef7\u683c":
            return fields[0]
        return None

    @staticmethod
    def _displayed_security_name(root: AXNode) -> str:
        """Read the security name paired with the trade form's code label."""
        nodes = list(root.walk())
        for index, node in enumerate(nodes):
            if node.role not in {"AXStaticText", "AXText", "AXLabel"}:
                continue
            if node.text().strip() != "代码":
                continue
            if index + 1 >= len(nodes):
                return ""
            candidate = nodes[index + 1]
            text = candidate.text().strip()
            if (
                candidate.role in {"AXStaticText", "AXText", "AXLabel"}
                and text not in {"", "价格", "数量"}
            ):
                return text
            return ""
        return ""

    @classmethod
    def _quote_identity_matches(
        cls,
        root: AXNode,
        symbol: str,
        previous_code: str,
        previous_name: str,
    ) -> bool:
        code = cls._form_field(root, "代码")
        current_name = cls._displayed_security_name(root)
        if code is None or code.text().strip() != symbol or not current_name:
            return False
        return not (
            previous_code
            and previous_code != symbol
            and previous_name
            and current_name == previous_name
        )

    @staticmethod
    def _normalise_number(value: str) -> float | None:
        try:
            return float(value.replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _order_book_complete(book: OrderBook) -> bool:
        return len(book.levels) >= 10 and all(
            level.price is not None and level.quantity is not None
            for level in book.levels
        )

    def _live_text(self, node: AXNode) -> str:
        for attribute in ("AXValue", "AXTitle", "AXDescription"):
            value = self.client.read_attribute(node, attribute)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _wait_live_value(
        self,
        node: AXNode,
        expected: str,
        *,
        numeric: bool = False,
        timeout: float = 1.5,
    ) -> None:
        deadline = time.monotonic() + timeout
        expected_number = self._normalise_number(expected) if numeric else None
        while time.monotonic() < deadline:
            actual = self._live_text(node)
            if numeric:
                actual_number = self._normalise_number(actual)
                if (
                    expected_number is not None
                    and actual_number is not None
                    and abs(expected_number - actual_number) < 0.0001
                ):
                    return
            elif actual == expected:
                return
            time.sleep(0.05)
        actual = self._live_text(node)
        raise DataValidationError(
            f"trade form field did not retain value {expected!r}; actual={actual!r}"
        )

    def _write_live_field(
        self, field: AXNode, label: str, value: str, *, numeric: bool = False
    ) -> None:
        if label == "代码":
            # AXValue + AXConfirm can change the displayed code without
            # firing Tonghuashun's quote refresh handler. A real text-change
            # keyboard event followed by focus loss is required for codes.
            self.client.set_value(field, "")
            self._wait_live_value(field, "", timeout=0.5)
            self.client.type_text(field, value)
            self._wait_live_value(field, value)
            if "AXConfirm" in self.client.action_names(field):
                self.client.perform_action(field, "AXConfirm")
            self.client.press_tab()
            return

        # Native AXValue avoids the intermittent focus/paste loss seen after
        # several consecutive submissions for price and quantity. Retain the
        # keyboard path only for client versions that reject writable AXValue.
        try:
            self.client.set_value(field, value)
            self._wait_live_value(field, value, numeric=numeric, timeout=0.5)
        except (AccessibilityUnavailable, DataValidationError):
            self.client.type_text(field, value)
            self._wait_live_value(field, value, numeric=numeric)
        actions = self.client.action_names(field)
        if "AXConfirm" in actions:
            self.client.perform_action(field, "AXConfirm")

    @staticmethod
    def _quote_price_buttons(root: AXNode, candidates: set[str]) -> list[AXNode]:
        buttons: list[AXNode] = []
        for table in (node for node in root.walk() if node.role == "AXTable"):
            table_text = " ".join(node.text() for node in table.walk() if node.text())
            if "买" not in table_text and "卖" not in table_text:
                continue
            buttons.extend(
                node for node in table.walk()
                if node.role == "AXButton" and node.enabled and node.text().strip() in candidates
            )
        return buttons

    def _set_form_field(self, label: str, value: str, *, numeric: bool = False) -> None:
        root = self._snapshot()
        try:
            field = self._form_field(root, label)
            if field is None:
                raise DataValidationError(f"trade form field was not found: {label}")
            self.client.type_text(field, value)
            self._wait_live_value(field, value, numeric=numeric)
            if "AXConfirm" in self.client.action_names(field):
                self.client.perform_action(field, "AXConfirm")
            if label == "\u4ee3\u7801":
                self.client.press_tab()
        finally:
            root.release()
        time.sleep(0.35)

        check = self._snapshot()
        try:
            field = self._form_field(check, label)
            actual = field.text() if field is not None else ""
            if numeric:
                expected_number = self._normalise_number(value)
                actual_number = self._normalise_number(actual)
                valid = (
                    expected_number is not None
                    and actual_number is not None
                    and abs(expected_number - actual_number) < 0.0001
                )
            else:
                valid = actual.strip() == value
            if not valid:
                raise DataValidationError(
                    f"trade form field {label} did not retain value {value!r}; actual={actual!r}"
                )
        finally:
            check.release()

    def _set_price(self, price: float) -> None:
        formatted = f"{price:.3f}"
        root = self._snapshot()
        try:
            field = self._form_field(root, "\u4ef7\u683c")
            if field is not None:
                self.client.type_text(field, formatted)
                if "AXConfirm" in self.client.action_names(field):
                    self.client.perform_action(field, "AXConfirm")
            else:
                candidates = {formatted, f"{price:.2f}", f"{price:g}"}
                button = next(iter(self._quote_price_buttons(root, candidates)), None)
                if button is None:
                    raise DataValidationError(
                        "trade form price control is not exposed; select a matching quote level first"
                    )
                self.client.press(button)
        finally:
            root.release()
        time.sleep(0.35)

        check = self._snapshot()
        try:
            price_field = self._form_field(check, "价格")
            if price_field is not None:
                # A quote button elsewhere in the page may have the requested
                # price even when the editable trade field kept an old value.
                # When a form field exists, only that field proves the value
                # that will actually be submitted.
                values = [price_field.text().strip()]
            else:
                values = [
                    node.text().strip()
                    for node in self._quote_price_buttons(
                        check, {formatted, f"{price:.2f}", f"{price:g}"}
                    )
                ]
            numeric_values = [self._normalise_number(value) for value in values]
            if not any(
                value is not None and abs(value - price) < 0.0001
                for value in numeric_values
            ):
                raise DataValidationError(
                    f"trade form price did not retain {price:.3f}; current price controls are unavailable"
                )
        finally:
            check.release()

    @staticmethod
    def _button(root: AXNode, *labels: str) -> AXNode | None:
        for label in labels:
            button = TonghuashunGuiGateway._find_button(root, label)
            if button is not None:
                return button
        return None

    @staticmethod
    def _contains_text(node: AXNode, value: str) -> bool:
        return any(
            value in text
            for item in (node.text(), node.title or "", node.value or "")
            for text in (item,)
        ) or any(TonghuashunGuiGateway._contains_text(child, value) for child in node.children)

    def _set_symbol(self, symbol: str) -> None:
        # The code field refreshes quotes on focus loss, but Tonghuashun can
        # treat a submit-style key as form submission when an old quantity is
        # still present. Clear the quantity first so read-only quote queries
        # cannot submit stale buy/sell orders.
        root = self._snapshot()
        try:
            quantity = self._form_field(root, "\u6570\u91cf")
            if quantity is not None and quantity.text().strip() not in {"", "0"}:
                # Empty paste is ignored by this custom control; zero is
                # accepted as an invalid-but-non-submittable quantity and is
                # replaced with the requested quantity during order submit.
                self.client.type_text(quantity, "0")
        finally:
            root.release()
        time.sleep(0.15)
        self._set_form_field("\u4ee3\u7801", symbol)
        time.sleep(0.45)

    def _select_trade_side(self, side: str) -> None:
        """Put the trade form in the requested direction before writing it."""
        label = "买入" if side == "buy" else "卖出"
        confirm_label = "确定买入" if side == "buy" else "确定卖出"
        root = self._snapshot()
        try:
            if self._button(root, confirm_label) is not None:
                return
            toggle = self._button(root, label)
            if toggle is None:
                raise DataValidationError(f"trade side button is unavailable: {side}")
            self.client.press(toggle)
        finally:
            root.release()

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            check = self._snapshot()
            try:
                if self._button(check, confirm_label) is not None:
                    return
            finally:
                check.release()
            time.sleep(0.05)
        raise DataValidationError(f"trade form did not switch to side: {side}")

    def _set_symbol_fast(self, symbol: str) -> OrderBook:
        """Refresh a symbol and prove quote ownership before returning prices."""
        last_error: DataValidationError | None = None
        for refresh_attempt in range(2):
            root: AXNode | None = None
            try:
                root = self._snapshot()
                quantity = self._form_field(root, "数量")
                code = self._form_field(root, "代码")
                if quantity is None or code is None:
                    raise DataValidationError("trade form fields are not fully exposed")
                previous_code = code.text().strip()
                previous_name = self._displayed_security_name(root)
                if (
                    self._last_quote_identity is not None
                    and self._last_quote_identity[0] != symbol
                ):
                    previous_code, previous_name = self._last_quote_identity
                if quantity.text().strip() not in {"", "0"}:
                    self._write_live_field(quantity, "数量", "0", numeric=True)
                self._write_live_field(code, "代码", symbol)
            except (DataValidationError, AccessibilityUnavailable):
                if refresh_attempt == 1:
                    raise
                continue
            finally:
                if root is not None:
                    root.release()

            # A retained quote table can stay alive and complete while still
            # containing the previous symbol's prices. Full-window reads are
            # required, and a cross-symbol transition must change the name.
            deadline = time.monotonic() + 3.5
            while time.monotonic() < deadline:
                check: AXNode | None = None
                try:
                    check = self._snapshot()
                    identity_ready = self._quote_identity_matches(
                        check, symbol, previous_code, previous_name
                    )
                    book = parse_order_book(check, symbol, now=datetime.now())
                    if identity_ready and self._order_book_complete(book):
                        self._last_quote_identity = (
                            symbol, self._displayed_security_name(check)
                        )
                        return book
                except DataValidationError as exc:
                    last_error = exc
                finally:
                    if check is not None:
                        check.release()
                time.sleep(0.05)
        raise last_error or DataValidationError(
            f"order book did not finish switching to symbol {symbol}"
        )

    def _set_trade_form(self, symbol: str, price: float, quantity: int, side: str) -> None:
        self._select_trade_side(side)
        self._set_symbol(symbol)
        self._fill_trade_form(price, quantity)

    def _fill_trade_form(self, price: float, quantity: int) -> None:
        """Fill a form whose symbol and quote page were already refreshed."""
        self._set_price(price)
        self._set_form_field("\u6570\u91cf", str(quantity), numeric=True)

    def _fill_trade_form_fast(self, preview: OrderPreview) -> None:
        """Fill and submit using a fresh form snapshot plus live field reads."""
        root = self._snapshot()
        try:
            price = self._form_field(root, "价格")
            quantity = self._form_field(root, "数量")
            button = self._button(
                root, "确定买入" if preview.side == "buy" else "确定卖出"
            )
            if price is None or quantity is None or button is None:
                raise DataValidationError("trade form controls are not fully exposed")
            self._write_live_field(price, "价格", f"{preview.price:.3f}", numeric=True)
            self._write_live_field(quantity, "数量", str(preview.quantity), numeric=True)
            self.client.press(button)
        finally:
            root.release()

    @staticmethod
    def _latest_matching_order(matches: list[Order]) -> list[Order]:
        if not matches:
            return []

        def key(order: Order) -> tuple[str, int | str]:
            order_id = order.order_id or ""
            try:
                order_key: int | str = int(order_id)
            except ValueError:
                order_key = order_id
            return (order.order_time or "", order_key)

        return [max(matches, key=key)]

    @classmethod
    def _matching_submitted_orders(
        cls,
        orders: list[Order],
        preview: OrderPreview,
        submitted_after: datetime,
    ) -> list[Order]:
        recent: list[Order] = []
        not_before = submitted_after - timedelta(seconds=2)
        for order in orders:
            if (
                order.symbol != preview.symbol
                or order.quantity != preview.quantity
                or order.price is None
                or abs(order.price - preview.price) >= 0.0001
                or not cls._order_side_matches(order, preview.side)
                or not order.order_time
            ):
                continue
            try:
                order_time = datetime.strptime(order.order_time, "%Y%m%d %H:%M:%S")
            except ValueError:
                continue
            if order_time >= not_before:
                recent.append(order)
        return cls._latest_matching_order(recent)

    def _press_order_confirmation(self, preview: OrderPreview) -> str | None:
        """Press the first submit button and resolve the immediate dialog once."""
        root = self._snapshot()
        try:
            confirm_label = "\u786e\u5b9a\u4e70\u5165" if preview.side == "buy" else "\u786e\u5b9a\u5356\u51fa"
            button = self._button(root, confirm_label)
            if button is None:
                raise DataValidationError(f"confirmation button is unavailable: {confirm_label}")
            self.client.press(button)
        finally:
            root.release()
        return self._confirm_dialog(preview)

    def _confirm_dialog(self, preview: OrderPreview) -> str | None:
        """Handle the post-submit dialog and return a broker rejection message.

        Tonghuashun can show a warning dialog after the order form was
        accepted locally. Clicking the initial "确定买入/卖出" button is not
        enough to call the order accepted: the warning must be closed and
        surfaced to the caller as a rejected submission.
        """
        rejection_message: str | None = None
        normal_confirmation_clicked = False
        root = self._snapshot()
        try:
            text = " ".join(node.text() for node in root.walk() if node.text())
            confirm = self._button(root, "确认", "确定")
            cancel = self._button(root, "取消")
            has_order_confirmation = (
                "是否确认" in text
                or "确认以上" in text
                or (confirm is not None and cancel is not None and "股东账号" in text)
            )
            # The normal order confirmation dialog is also titled "警告".
            # Initialization errors are unambiguous; other warning dialogs
            # count as broker rejections only when they contain no order
            # confirmation content.
            is_initialization_error = any(
                marker in text for marker in ("证券交易未初始化", "交易未初始化")
            )
            is_business_warning = (
                is_initialization_error or ("警告" in text and not has_order_confirmation)
            )
            if is_business_warning:
                if confirm is None:
                    raise DataValidationError("Tonghuashun warning dialog has no confirmation button")
                self.client.press(confirm)
                rejection_message = f"Tonghuashun rejected order: {text}"
            else:
                has_dialog = has_order_confirmation
                if has_dialog:
                    if preview.symbol not in text or str(preview.quantity) not in text:
                        cancel = self._button(root, "取消")
                        if cancel is not None:
                            self.client.press(cancel)
                        raise DataValidationError("order confirmation dialog does not match the preview")
                    price_values = [
                        self._normalise_number(token)
                        for token in text.replace("：", " ").replace(":", " ").split()
                    ]
                    price_values.extend(
                        self._normalise_number(match)
                        for match in re.findall(
                            r"(?:当前价格|委托价格|价格)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)",
                            text,
                        )
                    )
                    price_ok = any(
                        value is not None and abs(value - preview.price) < 0.0001
                        for value in price_values
                    )
                    if not price_ok:
                        cancel = self._button(root, "取消")
                        if cancel is not None:
                            self.client.press(cancel)
                        raise DataValidationError("order confirmation dialog price does not match the preview")
                    if confirm is None:
                        raise DataValidationError("order confirmation button is unavailable")
                    self.client.press(confirm)
                    normal_confirmation_clicked = True
        finally:
            root.release()
        time.sleep(0.6)
        if rejection_message is None and normal_confirmation_clicked:
            # A successful-looking confirmation can be followed by a broker
            # business-error dialog (for example, trading not initialized or
            # insufficient sellable quantity). Read the focused window again
            # before treating the GUI click as accepted.
            follow_up = self._snapshot()
            try:
                follow_up_text = " ".join(
                    node.text() for node in follow_up.walk() if node.text()
                )
                follow_up_confirm = self._button(follow_up, "确认", "确定")
                follow_up_has_order_confirmation = (
                    "是否确认" in follow_up_text
                    or "确认以上" in follow_up_text
                    or "股东账号" in follow_up_text
                )
                follow_up_is_warning = (
                    "证券交易未初始化" in follow_up_text
                    or "交易未初始化" in follow_up_text
                    or ("警告" in follow_up_text and not follow_up_has_order_confirmation)
                )
                if follow_up_is_warning:
                    if follow_up_confirm is None:
                        raise DataValidationError(
                            "Tonghuashun warning dialog has no confirmation button"
                        )
                    self.client.press(follow_up_confirm)
                    rejection_message = f"Tonghuashun rejected order: {follow_up_text}"
            finally:
                follow_up.release()
        return rejection_message

    @staticmethod
    def _close(root: AXNode, result):
        root.release()
        return result

    def get_connection_status(self) -> ConnectionStatus:
        checked_at = datetime.now()
        try:
            root = self._snapshot()
            try:
                text = " ".join(
                    value
                    for node in root.walk()
                    for value in (node.text(), node.help or "")
                    if value
                )
                mode = self._board
                if mode is None and "模拟" in text and "A股" not in text:
                    mode = "模拟"
                connected = "已连接" in text
                message = None if connected else "Tonghuashun does not report a connected state"
                return ConnectionStatus(
                    connected=connected,
                    bundle_id=self.settings.bundle_id,
                    process_id=self.client.process_id(),
                    account_mode=mode,
                    message=message,
                    checked_at=checked_at,
                )
            finally:
                root.release()
        except GatewayError as exc:
            return ConnectionStatus(
                connected=False,
                bundle_id=self.settings.bundle_id,
                message=str(exc),
                checked_at=checked_at,
            )

    def get_account_funds(self) -> AccountFunds:
        root = self._snapshot()
        try:
            return parse_funds(root, now=datetime.now())
        finally:
            root.release()

    def get_positions(self) -> list[Position]:
        root = self._select_tab("\u6301\u4ed3")
        try:
            return parse_positions(root)
        finally:
            root.release()

    def get_orders(self) -> list[Order]:
        root = self._select_tab("\u59d4\u6258")
        try:
            return parse_orders(root)
        finally:
            root.release()

    def get_trades(self) -> list[Trade]:
        root = self._select_tab("\u6210\u4ea4")
        try:
            return parse_trades(root)
        finally:
            root.release()

    def get_fund_details(self, start_date: date | None, end_date: date | None) -> list[FundDetail]:
        # Date filtering is intentionally left to the client until the GUI date controls
        # have been mapped. We still expose the requested range in the API contract.
        del start_date, end_date
        root = self._select_tab("\u8d44\u91d1\u660e\u7ec6")
        try:
            return parse_fund_details(root)
        finally:
            root.release()

    def get_order_book(self, symbol: str, board: str = "A股") -> OrderBook:
        if not symbol.isdigit() or len(symbol) != 6:
            raise DataValidationError("symbol must be a six-digit code")
        self._select_board(board)
        return self._set_symbol_fast(symbol)

    def _account_snapshot(self) -> tuple[ConnectionStatus, AccountFunds, list[Position]]:
        status = self.get_connection_status()
        if not status.connected:
            raise GatewayError(status.message or "Tonghuashun is not connected")
        return status, self.get_account_funds(), self.get_positions()

    @staticmethod
    def _amount_quantity(amount: float, price: float) -> int:
        """Return the largest A-share board-lot quantity within ``amount``."""
        lots = (Decimal(str(amount)) / Decimal(str(price)) / Decimal("100")).to_integral_value(
            rounding=ROUND_DOWN
        )
        return int(lots * 100)

    @staticmethod
    def _opposing_level(book: OrderBook, side: str, level: int):
        quote_side = "sell" if side == "buy" else "buy"
        quote = next(
            (item for item in book.levels if item.side == quote_side and item.level == level),
            None,
        )
        if quote is None or quote.price is None or quote.price <= 0:
            raise DataValidationError(
                f"opposing quote is unavailable: {quote_side} level {level}"
            )
        return quote

    def _create_order_preview(
        self,
        status: ConnectionStatus,
        funds: AccountFunds,
        positions: list[Position],
        book: OrderBook,
        symbol: str,
        side: str,
        price: float,
        quantity: int,
    ) -> OrderPreview:
        estimated_amount = round(price * quantity, 2)
        warnings = list(book.warnings)
        sellable = next((p.sellable_quantity for p in positions if p.symbol == symbol), None)
        if side == "buy" and funds.available_cash is not None and estimated_amount > funds.available_cash:
            warnings.append("estimated amount exceeds available cash")
        if side == "sell" and (sellable is None or quantity > sellable):
            warnings.append("quantity exceeds sellable quantity")

        preview_id = uuid4().hex
        preview = OrderPreview(
            preview_id=preview_id,
            expires_at=datetime.now() + timedelta(seconds=self.settings.preview_ttl_seconds),
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            order_type="limit",
            account_mode=status.account_mode,
            available_cash=funds.available_cash,
            sellable_quantity=sellable,
            estimated_amount=estimated_amount,
            order_book=book,
            warnings=warnings,
        )
        self._order_previews[preview_id] = preview
        return preview

    def preview_order(
        self, symbol: str, side: str, price: float, quantity: int, board: str = "A股"
    ) -> OrderPreview:
        if side not in {"buy", "sell"}:
            raise DataValidationError("side must be buy or sell")
        if not symbol.isdigit() or len(symbol) not in {6}:
            raise DataValidationError("symbol must be a six-digit code")
        if price <= 0 or quantity <= 0:
            raise DataValidationError("price and quantity must be positive")
        if quantity % 100 != 0:
            raise DataValidationError("quantity must be a multiple of 100")

        self._select_board(board)
        status, funds, positions = self._account_snapshot()
        book = self.get_order_book(symbol, board)
        return self._create_order_preview(
            status, funds, positions, book, symbol, side, price, quantity
        )

    def _order_preview(self, preview_id: str) -> OrderPreview:
        preview = self._order_previews.get(preview_id)
        if preview is None or preview.expires_at < datetime.now():
            self._order_previews.pop(preview_id, None)
            raise DataValidationError("order preview is missing or expired")
        return preview

    def _ensure_order_actions_allowed(self, account_mode: str | None) -> None:
        if not self.settings.allow_order_actions:
            raise GatewayError("order actions are disabled; set THSGUI_ALLOW_ORDER_ACTIONS=true")
        if account_mode != "模拟" and not self.settings.allow_live_orders:
            raise GatewayError("live account actions are disabled")

    def submit_order(self, preview_id: str, confirm: bool) -> OrderSubmitResult:
        preview = self._order_preview(preview_id)
        if not confirm:
            raise DataValidationError("confirm=true is required to submit an order")
        self._ensure_order_actions_allowed(preview.account_mode)

        self._select_board(preview.account_mode or "A股")
        submitted_after = datetime.now()
        self._set_trade_form(preview.symbol, preview.price, preview.quantity, preview.side)
        rejection_message = self._press_order_confirmation(preview)
        if rejection_message is not None:
            self._order_previews.pop(preview_id, None)
            return OrderSubmitResult(
                accepted=False,
                verified=False,
                message=rejection_message,
                preview=preview,
                matching_orders=[],
            )
        matching = self._matching_submitted_orders(
            self.get_orders(), preview, submitted_after
        )
        self._order_previews.pop(preview_id, None)
        return OrderSubmitResult(
            accepted=True,
            verified=bool(matching),
            message="order submitted; verification depends on the refreshed order table",
            preview=preview,
            matching_orders=matching,
        )

    def execute_order(
        self,
        symbol: str,
        side: str,
        amount: float | None,
        quantity: int | None,
        price_level: int,
        max_price: float | None,
        min_price: float | None,
        confirm: bool,
        board: str = "A股",
    ) -> OrderExecuteResult:
        """Execute an order intent without exposing an expiring preview ID.

        This is one GUI action sequence, never an automatic retry mechanism.
        After the first submit click, a timeout must be reconciled with the
        order table before another request is issued.
        """
        if not confirm:
            raise DataValidationError("confirm=true is required to execute an order")
        if side not in {"buy", "sell"}:
            raise DataValidationError("side must be buy or sell")
        if board not in self._BOARDS:
            raise DataValidationError(f"unsupported board: {board}")
        if not symbol.isdigit() or len(symbol) != 6:
            raise DataValidationError("symbol must be a six-digit code")
        if not 1 <= price_level <= 5:
            raise DataValidationError("price_level must be between 1 and 5")
        if (amount is None) == (quantity is None):
            raise DataValidationError("exactly one of amount or quantity is required")
        if amount is not None and amount <= 0:
            raise DataValidationError("amount must be positive")
        if quantity is not None and (quantity <= 0 or quantity % 100 != 0):
            raise DataValidationError("quantity must be a positive multiple of 100")
        if side == "buy" and min_price is not None:
            raise DataValidationError("min_price is only valid for sell orders")
        if side == "sell" and max_price is not None:
            raise DataValidationError("max_price is only valid for buy orders")

        started = perf_counter()
        account_started = perf_counter()
        self._select_board(board)
        funds = self.get_account_funds()
        # Selecting the requested board and reading funds both prove the client
        # is available; avoid a second full-tree connection snapshot here.
        status = ConnectionStatus(
            connected=True,
            bundle_id=self.settings.bundle_id,
            process_id=self.client.process_id(),
            account_mode=board,
            checked_at=datetime.now(),
        )
        # A buy only needs available cash. Avoid a page transition that adds
        # latency and would require returning to the trading form afterward.
        positions = self.get_positions() if side == "sell" else []
        account_check = round((perf_counter() - account_started) * 1000)
        self._ensure_order_actions_allowed(status.account_mode)

        quote_started = perf_counter()
        if side == "sell":
            # get_positions() switches to its table. Return to the requested
            # trading before locating the code field and refreshing the quote.
            self._select_board(board, force=False)
        self._select_trade_side(side)
        book = self._set_symbol_fast(symbol)
        quote_refresh = round((perf_counter() - quote_started) * 1000)

        quote = self._opposing_level(book, side, price_level)
        price = quote.price
        if side == "buy" and max_price is not None and price > max_price:
            raise DataValidationError(f"selected ask {price:.3f} exceeds max_price {max_price:.3f}")
        if side == "sell" and min_price is not None and price < min_price:
            raise DataValidationError(f"selected bid {price:.3f} is below min_price {min_price:.3f}")

        resolved_quantity = quantity
        if resolved_quantity is None:
            resolved_quantity = self._amount_quantity(amount, price)
            if resolved_quantity <= 0:
                raise DataValidationError("amount cannot cover one 100-share lot at the selected price")
        estimated_amount = round(price * resolved_quantity, 2)
        if side == "buy" and funds.available_cash is not None and estimated_amount > funds.available_cash:
            raise DataValidationError("estimated amount exceeds available cash")
        sellable = next((p.sellable_quantity for p in positions if p.symbol == symbol), None)
        if side == "sell" and (sellable is None or resolved_quantity > sellable):
            raise DataValidationError("quantity exceeds sellable quantity")

        preview = self._create_order_preview(
            status, funds, positions, book, symbol, side, price, resolved_quantity
        )
        try:
            fill_started = perf_counter()
            submitted_after = datetime.now()
            self._fill_trade_form_fast(preview)
            form_fill = round((perf_counter() - fill_started) * 1000)

            confirmation_started = perf_counter()
            rejection_message = self._confirm_dialog(preview)
            confirmation = round((perf_counter() - confirmation_started) * 1000)
            if rejection_message is not None:
                result = OrderSubmitResult(
                    accepted=False,
                    verified=False,
                    message=rejection_message,
                    preview=preview,
                    matching_orders=[],
                )
                order_refresh = 0
            else:
                refresh_started = perf_counter()
                matching = self._matching_submitted_orders(
                    self.get_orders(), preview, submitted_after
                )
                order_refresh = round((perf_counter() - refresh_started) * 1000)
                result = OrderSubmitResult(
                    accepted=True,
                    verified=bool(matching),
                    message="order submitted; verification depends on the refreshed order table",
                    preview=preview,
                    matching_orders=matching,
                )
            timings = ExecutionTimings(
                account_check=account_check,
                quote_refresh=quote_refresh,
                form_fill=form_fill,
                confirmation=confirmation,
                order_refresh=order_refresh,
                total=round((perf_counter() - started) * 1000),
            )
            return OrderExecuteResult(
                **result.model_dump(),
                price_level=price_level,
                requested_amount=amount,
                timings_ms=timings,
            )
        finally:
            self._order_previews.pop(preview.preview_id, None)

    @staticmethod
    def _is_cancellable(order: Order) -> bool:
        if not order.status:
            return True
        status = order.status.strip()
        # “未成交” is still cancellable. Only explicit terminal states are
        # rejected; checking for the single character “成” would incorrectly
        # reject every unfilled order.
        return not any(
            marker in status
            for marker in ("已撤", "已成", "部分成交", "废单", "失效")
        )

    @staticmethod
    def _order_side_matches(order: Order, scope: str) -> bool:
        if scope == "all":
            return True
        side = (order.side or "").lower()
        return ("买" in side or "buy" in side) if scope == "buy" else (
            "卖" in side or "sell" in side
        )

    def preview_cancel(self, order_id: str, board: str = "A股") -> CancelPreview:
        if not order_id.strip():
            raise DataValidationError("order_id is required")
        self._select_board(board)
        orders = self.get_orders()
        order = next((item for item in orders if item.order_id == order_id), None)
        if order is None:
            raise DataValidationError(f"order was not found: {order_id}")
        if not self._is_cancellable(order):
            raise DataValidationError(f"order is not cancellable: {order.status}")
        status = self.get_connection_status()
        preview_id = uuid4().hex
        preview = CancelPreview(
            preview_id=preview_id,
            expires_at=datetime.now() + timedelta(seconds=self.settings.preview_ttl_seconds),
            order=order,
            account_mode=board,
        )
        self._cancel_previews[preview_id] = preview
        return preview

    def preview_cancel_batch(self, scope: str = "all", board: str = "A股") -> CancelBatchPreview:
        if scope not in {"buy", "sell", "all"}:
            raise DataValidationError("scope must be buy, sell or all")
        self._select_board(board)
        orders = [
            order for order in self.get_orders()
            if order.order_id and self._is_cancellable(order)
            and self._order_side_matches(order, scope)
        ]
        if not orders:
            raise DataValidationError(f"no cancellable {scope} orders were found")
        status = self.get_connection_status()
        preview_id = uuid4().hex
        preview = CancelBatchPreview(
            preview_id=preview_id,
            expires_at=datetime.now() + timedelta(seconds=self.settings.preview_ttl_seconds),
            scope=scope,
            orders=orders,
            account_mode=board,
        )
        self._cancel_batch_previews[preview_id] = preview
        return preview

    def _select_cancel_rows(
        self, root: AXNode, order_ids: list[str], scope: str
    ) -> None:
        tables = [node for node in root.walk() if node.role == "AXTable"]
        order_table = next(
            (
                table
                for table in tables
                if all(
                    any(
                        row.role == "AXRow" and self._contains_text(row, order_id)
                        for row in table.children
                    )
                    for order_id in order_ids
                )
            ),
            None,
        )
        if order_table is None:
            raise DataValidationError("order table is not available for cancellation")
        # AXSelected=false updates a row's exposed attribute but does not
        # reliably clear Tonghuashun's internal AXSelectedRows collection.
        # Reset every table-level collection before invoking a range action.
        for table in tables:
            self.client.clear_selected_rows(table)
        rows = [row for row in order_table.children if row.role == "AXRow"]
        targets = [
            next((row for row in rows if self._contains_text(row, order_id)), None)
            for order_id in order_ids
        ]
        if any(target is None for target in targets):
            missing = [order_id for order_id, target in zip(order_ids, targets) if target is None]
            raise DataValidationError(f"target order row is no longer visible: {','.join(missing)}")
        # The native table is single-select and the three toolbar actions are
        # range actions (撤买, 撤卖, 全撤). A row selection cannot scope them to
        # one order, so only make a best-effort selection for single-order
        # diagnostics; the confirmation count remains the safety boundary.
        if len(targets) == 1:
            assert targets[0] is not None
            self.client.select(targets[0])
        time.sleep(0.2)
        button_labels = {
            "buy": ("撤买", "全撤买", "撤买全撤"),
            "sell": ("撤卖", "全撤卖", "撤卖全撤"),
            "all": ("全撤", "全部撤单", "全撤单", "全撤买卖", "撤单"),
        }[scope]
        button = self._button(root, *button_labels)
        if button is None:
            raise DataValidationError(f"cancel button is unavailable: {scope}")
        self.client.press(button)

    def _fresh_orders_root(self) -> AXNode:
        """Rebuild the order table so stale multi-selection cannot leak."""
        stale_selection = self._select_tab("持仓")
        stale_selection.release()
        return self._select_tab("委托")

    def submit_cancel(self, preview_id: str, confirm: bool) -> CancelSubmitResult:
        preview = self._cancel_previews.get(preview_id)
        if preview is None or preview.expires_at < datetime.now():
            self._cancel_previews.pop(preview_id, None)
            raise DataValidationError("cancel preview is missing or expired")
        if not confirm:
            raise DataValidationError("confirm=true is required to cancel an order")
        self._ensure_order_actions_allowed(preview.account_mode)

        started = perf_counter()
        selection_started = perf_counter()
        self._select_board(preview.account_mode or "A股")
        # Tonghuashun can keep stale multi-selection state when the same
        # 委托 tab is revisited. Recreate the table before selecting one row;
        # the confirmation count remains the final safety check.
        root = self._fresh_orders_root()
        try:
            if not preview.order.order_id:
                raise DataValidationError("order preview has no order_id")
            side_scope = "buy" if self._order_side_matches(preview.order, "buy") else "sell"
            self._select_cancel_rows(root, [preview.order.order_id], side_scope)
        finally:
            root.release()
        selection = round((perf_counter() - selection_started) * 1000)
        confirmation_started = perf_counter()
        rejection_message = self._confirm_cancel_dialog(preview, expected_count=1)
        confirmation = round((perf_counter() - confirmation_started) * 1000)
        if rejection_message is not None:
            self._cancel_previews.pop(preview_id, None)
            return CancelSubmitResult(
                accepted=False, verified=False, message=rejection_message,
                preview=preview, matching_orders=[],
                timings_ms=CancelTimings(selection=selection, confirmation=confirmation, order_refresh=0, total=round((perf_counter() - started) * 1000)),
            )
        refresh_started = perf_counter()
        matching = [order for order in self.get_orders() if order.order_id == preview.order.order_id]
        order_refresh = round((perf_counter() - refresh_started) * 1000)
        self._cancel_previews.pop(preview_id, None)
        verified = not matching or bool(matching[0].status and "撤" in matching[0].status)
        return CancelSubmitResult(
            accepted=True, verified=verified,
            message="cancel submitted; verification depends on the refreshed order table",
            preview=preview, matching_orders=matching,
            timings_ms=CancelTimings(selection=selection, confirmation=confirmation, order_refresh=order_refresh, total=round((perf_counter() - started) * 1000)),
        )

    def submit_cancel_batch(self, preview_id: str, confirm: bool) -> CancelBatchSubmitResult:
        preview = self._cancel_batch_previews.get(preview_id)
        if preview is None or preview.expires_at < datetime.now():
            self._cancel_batch_previews.pop(preview_id, None)
            raise DataValidationError("cancel batch preview is missing or expired")
        if not confirm:
            raise DataValidationError("confirm=true is required to cancel orders")
        self._ensure_order_actions_allowed(preview.account_mode)

        started = perf_counter()
        selection_started = perf_counter()
        self._select_board(preview.account_mode or "A股")
        root = self._fresh_orders_root()
        try:
            self._select_cancel_rows(
                root, [order.order_id or "" for order in preview.orders], preview.scope
            )
        finally:
            root.release()
        selection = round((perf_counter() - selection_started) * 1000)
        confirmation_started = perf_counter()
        rejection_message = self._confirm_cancel_dialog(
            preview, expected_count=len(preview.orders)
        )
        confirmation = round((perf_counter() - confirmation_started) * 1000)
        if rejection_message is not None:
            self._cancel_batch_previews.pop(preview_id, None)
            return CancelBatchSubmitResult(
                accepted=False, verified=False, message=rejection_message,
                preview=preview, matching_orders=[],
                timings_ms=CancelTimings(selection=selection, confirmation=confirmation, order_refresh=0, total=round((perf_counter() - started) * 1000)),
            )
        refresh_started = perf_counter()
        target_ids = {order.order_id for order in preview.orders}
        matching = [order for order in self.get_orders() if order.order_id in target_ids]
        order_refresh = round((perf_counter() - refresh_started) * 1000)
        self._cancel_batch_previews.pop(preview_id, None)
        verified = all(
            order.order_id not in target_ids or (order.status and "撤" in order.status)
            for order in matching
        )
        return CancelBatchSubmitResult(
            accepted=True, verified=verified,
            message="batch cancel submitted; verification depends on the refreshed order table",
            preview=preview, matching_orders=matching,
            timings_ms=CancelTimings(selection=selection, confirmation=confirmation, order_refresh=order_refresh, total=round((perf_counter() - started) * 1000)),
        )

    def _confirm_cancel_dialog(self, preview, expected_count: int) -> str | None:
        """Validate and accept the second confirmation after clicking a cancel button."""
        root = self._snapshot()
        try:
            text = " ".join(node.text() for node in root.walk() if node.text())
            confirm_button = self._button(root, "确认", "确定")
            cancel_button = self._button(root, "取消")
            match = re.search(r"撤(?:销|单).*?(\d+)\s*笔", text)
            actual_count = int(match.group(1)) if match else None
            has_cancel_confirmation = (
                ("撤单" in text or "撤销" in text)
                and actual_count == expected_count
            )
            is_cancel_dialog = "撤单委托" in text or "撤销" in text
            is_warning = "警告" in text and not is_cancel_dialog
            if is_warning:
                if confirm_button is not None:
                    self.client.press(confirm_button)
                return f"Tonghuashun rejected cancel: {text}"
            if not has_cancel_confirmation:
                if cancel_button is not None:
                    self.client.press(cancel_button)
                if is_cancel_dialog and actual_count is not None:
                    return f"cancel confirmation count mismatch: expected {expected_count}, got {actual_count}"
                raise DataValidationError("cancel confirmation dialog was not exposed")
            if confirm_button is None:
                raise DataValidationError("cancel confirmation button is unavailable")
            self.client.press(confirm_button)
        finally:
            root.release()
        time.sleep(0.6)
        follow_up = self._snapshot()
        try:
            follow_up_text = " ".join(node.text() for node in follow_up.walk() if node.text())
            follow_up_confirm = self._button(follow_up, "确认", "确定")
            if "警告" in follow_up_text and "撤单" not in follow_up_text:
                if follow_up_confirm is not None:
                    self.client.press(follow_up_confirm)
                return f"Tonghuashun rejected cancel: {follow_up_text}"
        finally:
            follow_up.release()
        return None
