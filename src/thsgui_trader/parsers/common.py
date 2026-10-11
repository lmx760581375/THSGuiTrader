from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable

from ..accessibility import AXNode


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def parse_text(value: object) -> str:
    return str(value or "").strip()


def parse_number(value: object) -> float | None:
    text = parse_text(value).replace(",", "").replace("，", "")
    if not text or text in {"-", "--", "—", "N/A", "null"}:
        return None
    text = text.replace("%", "")
    multiplier = Decimal("1")
    if text.endswith("万"):
        multiplier = Decimal("10000")
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = Decimal("100000000")
        text = text[:-1]
    text = re.sub(r"[^0-9+.\\-]", "", text)
    if text in {"", "+", "-", ".", "+.", "-."}:
        return None
    try:
        return float(Decimal(text) * multiplier)
    except InvalidOperation:
        return None


def _node_text(node: AXNode) -> str:
    direct = parse_text(node.text())
    if direct:
        return direct
    return " ".join(text for text in (_node_text(child) for child in node.children) if text)


def _direct_rows(table: AXNode) -> list[AXNode]:
    rows: list[AXNode] = []
    for node in table.walk():
        if node is not table and node.role == "AXRow":
            rows.append(node)
    return rows


def _row_cells(row: AXNode) -> list[str]:
    cells: list[str] = []
    for child in row.children:
        if child.role in {"AXCell", "AXColumn", "AXStaticText"}:
            text = _node_text(child)
            cells.append(text)
    if cells:
        return cells
    text = _node_text(row)
    return [text] if text else []


def _headers(table: AXNode) -> list[str]:
    headers: list[str] = []
    for node in table.walk():
        if node.role in {"AXSortButton", "AXColumn"}:
            text = _node_text(node)
            if text and text not in headers:
                headers.append(text)
    return headers


def table_from_node(table: AXNode) -> Table:
    return Table(
        headers=tuple(_headers(table)),
        rows=tuple(tuple(_row_cells(row)) for row in _direct_rows(table)),
    )


def find_tables(root: AXNode) -> Iterable[Table]:
    for node in root.walk():
        if node.role == "AXTable":
            yield table_from_node(node)
