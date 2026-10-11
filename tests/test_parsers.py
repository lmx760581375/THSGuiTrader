from datetime import datetime

from thsgui_trader.accessibility import AXNode
from thsgui_trader.models import AccountFunds
from thsgui_trader.parsers import parse_funds, parse_order_book, parse_orders, parse_positions


def cell(text: str) -> AXNode:
    return AXNode(role="AXCell", children=[AXNode(role="AXStaticText", value=text)])


def row(*values: str) -> AXNode:
    return AXNode(role="AXRow", children=[cell(value) for value in values])


def table(headers: list[str], rows: list[list[str]]) -> AXNode:
    return AXNode(
        role="AXTable",
        children=[
            *[AXNode(role="AXSortButton", title=header) for header in headers],
            *[row(*values) for values in rows],
        ],
    )


def test_parse_funds_and_reconciliation_warning():
    root = AXNode(
        children=[
            AXNode(
                role="AXTable",
                children=[
                    row("总资产", "307176.92"),
                    row("总市值", "302090.00"),
                    row("总盈亏", "88211.26"),
                    row("当日盈亏", "11050.00"),
                    row("资金余额", "5086.92"),
                    row("可取金额", "0.00"),
                    row("可用金额", "5086.92"),
                ],
            )
        ]
    )
    result = parse_funds(root, now=datetime(2026, 1, 1))
    assert isinstance(result, AccountFunds)
    assert result.total_assets == 307176.92
    assert result.market_value == 302090.0
    assert result.warnings == []


def test_parse_positions_uses_semantic_headers():
    root = AXNode(
        children=[
            table(
                [
                    "证券代码",
                    "证券名称",
                    "市价",
                    "盈亏",
                    "实际数量",
                    "股票余额",
                    "可用余额",
                    "成本价",
                    "市值",
                    "交易市场",
                    "股东账户",
                ],
                [[
                    "603876",
                    "鼎胜新材",
                    "17.770",
                    "88211.260",
                    "17000",
                    "17000",
                    "17000",
                    "12.581",
                    "302090.000",
                    "上海Ａ股",
                    "A534079722",
                ]],
            )
        ]
    )
    result = parse_positions(root)
    assert len(result) == 1
    assert result[0].symbol == "603876"
    assert result[0].quantity == 17000
    assert result[0].market_value == 302090.0


def test_parse_order_book_reads_five_levels_per_side():
    def book_row(side: str, price: str, quantity: str) -> AXNode:
        return AXNode(
            role="AXRow",
            children=[
                AXNode(
                    role="AXCell",
                    children=[
                        AXNode(role="AXStaticText", value=side),
                        AXNode(role="AXButton", title=price),
                        AXNode(role="AXStaticText", value=quantity),
                    ],
                )
            ],
        )

    root = AXNode(
        children=[
            AXNode(
                role="AXTable",
                children=[book_row("卖", "17.90", "500") for _ in range(5)],
            ),
            AXNode(
                role="AXTable",
                children=[book_row("买", "17.77", "600") for _ in range(5)],
            ),
        ]
    )
    result = parse_order_book(root, "603876", now=datetime(2026, 1, 1))
    assert len(result.levels) == 10
    assert result.levels[0].side == "sell"
    assert result.levels[0].level == 5
    assert result.levels[5].side == "buy"
    assert result.levels[5].price == 17.77


def test_parse_orders_accepts_tonghuashun_real_headers():
    root = AXNode(
        children=[
            table(
                [
                    "委托日期",
                    "委托时间",
                    "证券代码",
                    "证券名称",
                    "操作",
                    "备注",
                    "委托数量",
                    "撤销数量",
                    "委托价格",
                    "成交价格",
                    "合同编号",
                    "申报编号",
                    "委托属性",
                ],
                [[
                    "2026-10-11",
                    "09:31:00",
                    "603876",
                    "鼎胜新材",
                    "买入",
                    "已报",
                    "100",
                    "0",
                    "17.770",
                    "--",
                    "C123",
                    "R456",
                    "限价",
                ]],
            )
        ]
    )
    result = parse_orders(root)
    assert len(result) == 1
    assert result[0].order_id == "C123"
    assert result[0].symbol == "603876"
    assert result[0].side == "买入"
    assert result[0].price == 17.77
    assert result[0].quantity == 100
    assert result[0].status == "已报"


def test_parse_orders_accepts_flat_tonghuashun_order_nodes():
    values = [
        "20261011", "13:32:30", "000001", "平安银行", "证券买入", "未报",
        "100", "0", "11.610", "0.000", "635000314",
        "委托日期", "委托时间", "证券代码", "证券名称", "操作", "备注",
        "委托数量", "撤销数量", "委托价格", "成交价格", "合同编号",
    ]
    root = AXNode(children=[AXNode(role="AXStaticText", value=value) for value in values])
    result = parse_orders(root)
    assert len(result) == 1
    assert result[0].order_id == "635000314"
    assert result[0].symbol == "000001"
    assert result[0].quantity == 100
    assert result[0].status == "未报"
