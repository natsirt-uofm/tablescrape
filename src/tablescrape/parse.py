"""Turn the vision model's reply into clean, rectangular tables."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import json_repair


@dataclass
class Table:
    columns: list[str]
    rows: list[list[str]]
    label: str = ""
    caption: str = ""
    footnotes: str = ""
    continued: bool = False
    warnings: list[str] = field(default_factory=list)


def parse_reply(text: str) -> list[Table]:
    """The tables in a reply. Raises ValueError when the reply is not the JSON the prompt asks for."""
    data = _load_json(text)
    if isinstance(data, list):  # the model sometimes drops the {"tables": ...} wrapper
        data = {"tables": data}
    if data == {}:  # Qwen2.5-VL's way of saying "no tables" on some pages
        return []
    if not isinstance(data, dict) or not isinstance(data.get("tables"), list):
        raise ValueError("reply has no 'tables' list")
    tables = []
    for item in data["tables"]:
        if isinstance(item, dict):
            table = _clean_table(item)
            if table is not None:
                tables.append(table)
    return tables


_FIGURE = re.compile(r"^\s*(fig\.?|figure)\s*S?\d", re.IGNORECASE)


def is_figure(table: Table) -> bool:
    """A "table" labeled or captioned as a figure: a tree or diagram the model read as a grid of text."""
    return bool(_FIGURE.match(table.label) or _FIGURE.match(table.caption))


def _load_json(text: str) -> Any:
    body = text.strip()
    body = re.sub(r"^```(?:json)?\s*", "", body)  # a ```json fence, closed or not (a cut-off reply has no closing fence)
    body = re.sub(r"\s*```$", "", body)
    try:
        # numbers stay the exact text the model wrote: 1.10 must not become 1.1
        return json.loads(body, parse_float=str, parse_int=str)
    except json.JSONDecodeError:
        # closes the brackets of a reply that hit the token limit, drops trailing commas, and so on
        repaired = json_repair.loads(body)
        if repaired in ("", None, [], {}):
            raise ValueError("reply is not JSON") from None
        return repaired


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _clean_table(item: dict) -> Table | None:
    columns = [_cell(c) for c in _as_list(item.get("columns"))]
    rows = []
    for raw in _as_list(item.get("rows")):
        if isinstance(raw, dict):  # a row written as {"column": "value"}
            raw = [raw.get(c, "") for c in columns] if columns else list(raw.values())
        row = [_cell(c) for c in _as_list(raw)]
        if any(row):
            rows.append(row)
    if not rows and not any(columns):
        return None

    warnings = []
    width = max([len(columns)] + [len(r) for r in rows])
    short = sum(1 for r in rows if len(r) < width)
    if short:
        warnings.append(f"{short} row(s) had fewer cells than the widest row; padded with empty cells on the right")
    if columns and len(columns) < width:
        warnings.append(f"header has {len(columns)} names for {width} columns; extra columns named column_N")
    if not rows and any(columns):
        warnings.append("no data rows: the model probably wrote the whole table into the header; read it from the page image")
    columns += [""] * (width - len(columns))
    columns = [name or f"column_{i + 1}" for i, name in enumerate(columns)]  # a blank name would give a blank CSV line
    rows = [r + [""] * (width - len(r)) for r in rows]

    return Table(
        columns=columns,
        rows=rows,
        label=_cell(item.get("label")),
        caption=_cell(item.get("caption")),
        footnotes=_cell(item.get("footnotes")),
        continued=item.get("continued") in (True, "true", "True"),
        warnings=warnings,
    )
