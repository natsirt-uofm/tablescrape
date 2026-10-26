"""Build the outputs from the saved page readings: one workbook, one CSV per table, and an index.

OUT/tables.xlsx   sheet "Index" (one row per table, linked to its sheet), sheet "Documents" (one row per PDF, with or
                  without tables), sheet "Review" (every row with cells to check, with a crop of that row from the
                  page, present only when there is something to check), one sheet per table, sheet "Problems"
OUT/csv/<pdf key>/<table id>.csv   the table only: header row then data rows, cells exactly as read
OUT/index.csv, OUT/documents.csv   the Index and Documents sheets as CSV
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.comments import Comment
from openpyxl.drawing.image import Image as SheetImage
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from PIL import Image

from .pipeline import load_records
from .verify import flag_cells, verified_by

HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
CHECK_FILL = PatternFill("solid", fgColor="FFEB9C")  # yellow: the cell could not be confirmed
CLIPPED_FILL = PatternFill("solid", fgColor="F8CBAD")  # orange: cell is only the start of a longer value in the text
BOLD = Font(bold=True)
LINK = Font(color="0563C1", underline="single")
# a plain number as printed: no leading zeros (an ID like 007 stays text), at most 15 digits (Excel's precision)
NUMBER = re.compile(r"^-?(?:0|[1-9]\d{0,14})(?:\.(\d{1,15}))?$")

INDEX_COLUMNS = ["table_id", "sheet", "pdf", "page", "label", "caption", "continued", "n_rows", "n_cols",
                 "verified_by", "cells_to_check", "reread", "warnings", "csv", "page_image"]
REVIEW_COLUMNS = ["table", "page", "row", "cells to check", "read as", "OCR read this row as", "crop of the row"]
MAX_REVIEW_ROWS = 400
CROP_MAX_WIDTH, CROP_MAX_HEIGHT = 900, 170  # pixels of an embedded row crop
DOCUMENT_COLUMNS = ["pdf", "pages", "pages_read", "tables", "pages_with_errors", "status"]


@dataclass
class Entry:
    table_id: str
    sheet: str
    record: dict
    table: dict

    def index_row(self) -> dict:
        t, r = self.table, self.record
        warnings = list(t["warnings"])
        if r.get("truncated"):
            warnings.append("model reply was cut off at the token limit: rows at the end of this page may be missing")
        return {
            "table_id": self.table_id, "sheet": self.sheet, "pdf": r["pdf"], "page": r["page"],
            "label": t["label"], "caption": t["caption"], "continued": "yes" if t["continued"] else "",
            "n_rows": len(t["rows"]), "n_cols": len(t["columns"]), "verified_by": _verified_by(t),
            "cells_to_check": _cells_to_check(t), "reread": _reread(t), "warnings": "; ".join(warnings),
            "csv": f"csv/{r['key']}/{self.table_id}.csv", "page_image": f"pages/{r['key']}/p{r['page']:04d}.png",
        }


def _verified_by(table: dict) -> str:
    """What confirmed the table's cells: "text layer", "OCR" or "none" (blank for readings that predate the check)."""
    if "text_checked" not in table:
        return ""
    return table.get("verified_by") or verified_by(table)


def _reread(table: dict) -> str:
    meta = table.get("reread")
    if not meta:
        return ""
    return "from the picture" if meta["chosen"] == "picture" else "picture read, page reading kept"


def _cells_to_check(table: dict) -> str:
    """How many cells a person should compare with the page image (older readings have no check: blank)."""
    if "text_checked" not in table:
        return ""
    if _verified_by(table) == "none" and (table.get("picture_table") or not table["text_checked"]):
        return "all (picture table, nothing could confirm it)" if table.get("picture_table") else "all (no text layer)"
    return str(len(flag_cells(table)))


def build(out_dir: Path, numbers: bool = True) -> dict:
    pages, documents = load_records(out_dir)
    entries = []
    for record in pages:
        for n, table in enumerate(record["tables"], 1):
            table_id = f"{record['key']}_p{record['page']:03d}_t{n}"
            sheet = _sheet_name(len(entries) + 1, record)
            entries.append(Entry(table_id, sheet, record, table))

    summaries = _document_summaries(pages, documents)
    problems = [{"pdf": d["pdf"], "page": "", "problem": d["error"]} for d in documents if d.get("error")]
    for r in pages:
        if r.get("error"):
            problems.append({"pdf": r["pdf"], "page": r["page"], "problem": r["error"]})
        for note in r.get("verify_notes", []):
            problems.append({"pdf": r["pdf"], "page": r["page"], "problem": f"verification: {note}"})
        for name in r.get("skipped_figures", []):
            problems.append({"pdf": r["pdf"], "page": r["page"],
                             "problem": f"left out a figure the model read as a table: {name}"})
        if r.get("truncated"):
            problems.append({"pdf": r["pdf"], "page": r["page"],
                             "problem": "reply cut off at the token limit; rerun with a larger --max-new-tokens"})

    _write_csvs(out_dir, entries, summaries)
    _write_workbook(out_dir, entries, summaries, problems, numbers)
    return {"pdfs": len(summaries), "pages": len(pages), "tables": len(entries), "problems": len(problems)}


def _document_summaries(pages: list[dict], documents: list[dict]) -> list[dict]:
    """One row per PDF, including the ones with no tables, so every PDF in the folder is accounted for."""
    summaries = []
    for d in documents:
        mine = [r for r in pages if r["pdf"] == d["pdf"]]
        tables = sum(len(r["tables"]) for r in mine)
        failed = sum(1 for r in mine if r.get("error"))
        if d.get("error"):
            status = d["error"]
        elif len(mine) < d["planned"]:
            status = f"not finished: {len(mine)} of {d['planned']} pages read; run again to continue"
        elif tables == 0:
            status = "no tables found" + (f" ({failed} page(s) could not be read)" if failed else "")
        else:
            status = f"{tables} table(s)" + (f", {failed} page(s) could not be read" if failed else "")
        summaries.append({"pdf": d["pdf"], "pages": d["n_pages"], "pages_read": len(mine), "tables": tables,
                          "pages_with_errors": failed, "status": status})
    return summaries


def _sheet_name(n: int, record: dict) -> str:
    # Excel: at most 31 characters, none of []:*?/\ ; the running number keeps names unique after truncation
    name = f"T{n:03d} p{record['page']} {record['key']}"
    return re.sub(r"[\[\]:*?/\\]", "_", name)[:31]


def _write_csvs(out_dir: Path, entries: list[Entry], summaries: list[dict]) -> None:
    for e in entries:
        path = out_dir / e.index_row()["csv"]
        path.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig: Excel opens the file with ±, µ, Greek letters intact instead of guessing an old encoding
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(e.table["columns"])
            w.writerows(e.table["rows"])
    with (out_dir / "index.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_COLUMNS)
        w.writeheader()
        w.writerows(e.index_row() for e in entries)
    with (out_dir / "documents.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=DOCUMENT_COLUMNS)
        w.writeheader()
        w.writerows(summaries)


def _write_workbook(out_dir: Path, entries: list[Entry], summaries: list[dict], problems: list[dict],
                    numbers: bool) -> None:
    path = out_dir / "tables.xlsx"
    wb = Workbook()
    index = wb.active
    index.title = "Index"
    _header(index, 1, INDEX_COLUMNS)
    for row, e in enumerate(entries, 2):
        values = e.index_row()
        for col, name in enumerate(INDEX_COLUMNS, 1):
            _put(index.cell(row, col), values[name], numbers=False)
        _link(index.cell(row, 2), e.sheet)
    index.freeze_panes = "A2"
    _widths(index, cap=60)

    docs = wb.create_sheet("Documents")
    _header(docs, 1, DOCUMENT_COLUMNS)
    for row, s in enumerate(summaries, 2):
        for col, name in enumerate(DOCUMENT_COLUMNS, 1):
            _put(docs.cell(row, col), s[name], numbers=False)
    docs.freeze_panes = "A2"
    _widths(docs, cap=80)

    _write_review(wb, out_dir, entries)

    for e in entries:
        ws = wb.create_sheet(e.sheet)
        _link(ws.cell(1, 1, "<< Index"), "Index")
        meta = [("Table ID", e.table_id), ("Source PDF", e.record["pdf"]), ("Page", e.record["page"]),
                ("Label", e.table["label"]), ("Caption", e.table["caption"]), ("Footnotes", e.table["footnotes"]),
                ("Warnings", e.index_row()["warnings"]),
                ("Verified by", _verified_by(e.table) or "(not checked)"),
                ("Check", "Yellow cells could not be confirmed (not in the PDF's text, or OCR disagrees). Orange cells are "
                          "only the start of a longer value stored in the PDF. Hover a flagged cell for the reason. "
                          "The Review sheet shows a crop of each flagged row.")]
        for i, (name, value) in enumerate(meta, 2):
            ws.cell(i, 1, name).font = BOLD
            _put(ws.cell(i, 2), value, numbers=False)
        top = len(meta) + 3  # one empty row between the labels and the table
        _header(ws, top, e.table["columns"])
        for r, row in enumerate(e.table["rows"], top + 1):
            for c, value in enumerate(row, 1):
                _put(ws.cell(r, c), value, numbers)
        for (r, c), reason in flag_cells(e.table).items():
            cell = ws.cell(top + 1 + r, c + 1)
            cell.fill = CLIPPED_FILL if reason.startswith("only the start") else CHECK_FILL
            ocr_row = e.table.get("ocr_row_text", {}).get(str(r))
            cell.comment = Comment(reason + (f"\nOCR read the row as: {ocr_row}" if ocr_row else ""), "tablescrape")
        ws.freeze_panes = ws.cell(top + 1, 1)
        _widths(ws, cap=40, first_row=top)

    sheet = wb.create_sheet("Problems")
    _header(sheet, 1, ["pdf", "page", "problem"])
    for row, p in enumerate(problems, 2):
        for col, name in enumerate(["pdf", "page", "problem"], 1):
            _put(sheet.cell(row, col), p[name], numbers=False)
    _widths(sheet, cap=100)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _write_review(wb: Workbook, out_dir: Path, entries: list[Entry]) -> None:
    """One line per table row that has cells to check, with a crop of that row from the page, so a check is a glance."""
    lines = []
    for e in entries:
        by_row: dict[int, list[tuple[int, str]]] = {}
        for (r, c), reason in sorted(flag_cells(e.table).items()):
            by_row.setdefault(r, []).append((c, reason))
        for r, cells in sorted(by_row.items()):
            lines.append((e, r, cells))
    if not lines:
        return
    ws = wb.create_sheet("Review")
    _header(ws, 1, REVIEW_COLUMNS)
    widths = {1: 30, 2: 6, 3: 6, 4: 50, 5: 60, 6: 50, 7: 60}
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = width
    for row, (e, r, cells) in enumerate(lines[:MAX_REVIEW_ROWS], 2):
        names = e.table["columns"]
        shown = "\n".join(f"{names[c]}: {e.table['rows'][r][c]!r}  ({reason})" for c, reason in cells)
        _put(ws.cell(row, 1, e.table_id), e.table_id, numbers=False)
        _link(ws.cell(row, 1), e.sheet)
        ws.cell(row, 2, e.record["page"])
        ws.cell(row, 3, r + 1)
        _put(ws.cell(row, 4), shown, numbers=False)
        _put(ws.cell(row, 5), " | ".join(e.table["rows"][r]), numbers=False)
        _put(ws.cell(row, 6), e.table.get("ocr_row_text", {}).get(str(r), ""), numbers=False)
        for col in (4, 5, 6):
            ws.cell(row, col).alignment = Alignment(wrap_text=True, vertical="top")
        crop = _row_crop(out_dir, e, r)
        if crop is not None:
            picture = SheetImage(BytesIO(crop[0]))
            ws.add_image(picture, f"G{row}")
            ws.row_dimensions[row].height = max(30, crop[1] * 0.75 + 4)  # pixels -> points
    if len(lines) > MAX_REVIEW_ROWS:
        ws.cell(len(lines[:MAX_REVIEW_ROWS]) + 3, 1, f"... {len(lines) - MAX_REVIEW_ROWS} more rows not shown; "
                "see the table sheets")
    ws.freeze_panes = "A2"


def _row_crop(out_dir: Path, entry: Entry, row: int) -> tuple[bytes, int] | None:
    """A PNG of one table row cut from the saved page (or picture) image, and its height in pixels; None if unknown."""
    table = entry.table
    box = table.get("row_boxes", {}).get(str(row))
    name = table.get("review_image")
    if not box or not name:
        return None
    path = out_dir / "pages" / entry.record["key"] / name
    if not path.exists():
        return None
    with Image.open(path) as image:
        width, height = image.size
        if table.get("row_boxes_are_fractions"):
            box = [box[0] * width, box[1] * height, box[2] * width, box[3] * height]
        pad = max(6, 0.7 * (box[3] - box[1]))
        left, top = max(0, box[0] - 12), max(0, box[1] - pad)
        right, bottom = min(width, box[2] + 12), min(height, box[3] + pad)
        if right - left < 4 or bottom - top < 4:
            return None
        crop = image.convert("RGB").crop((round(left), round(top), round(right), round(bottom)))
    scale = min(1.0, CROP_MAX_WIDTH / crop.width, CROP_MAX_HEIGHT / crop.height)
    if scale < 1.0:
        crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))), Image.LANCZOS)
    out = BytesIO()
    crop.save(out, format="PNG")
    return out.getvalue(), crop.height


def _put(cell, value, numbers: bool) -> None:
    """Write one value. With numbers on, a plain number becomes a real Excel number that keeps its printed decimals."""
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)  # control characters Excel refuses to store
        m = NUMBER.match(value) if numbers else None
        if m:
            decimals = m.group(1)
            cell.value = float(value) if decimals else int(value)
            cell.number_format = "0." + "0" * len(decimals) if decimals else "0"
            return
    cell.value = value
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"  # a printed "=0.05" is text, not an Excel formula


def _header(ws, row: int, names: list[str]) -> None:
    for col, name in enumerate(names, 1):
        cell = ws.cell(row, col)
        _put(cell, name, numbers=False)
        cell.font = BOLD
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def _link(cell, sheet: str) -> None:
    cell.hyperlink = f"#'{sheet}'!A1"
    cell.font = LINK


def _widths(ws, cap: int, first_row: int = 1) -> None:
    widths: dict[int, int] = {}
    for row in ws.iter_rows(min_row=first_row):
        for cell in row:
            if cell.value is not None:
                widths[cell.column] = max(widths.get(cell.column, 0), len(str(cell.value)))
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = min(max(width + 2, 8), cap)
