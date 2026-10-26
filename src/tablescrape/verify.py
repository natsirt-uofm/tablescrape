"""Decide how far each table can be trusted, and try to improve the ones that cannot be verified.

For every table on a page this does, in order:

1. Look every cell up in the PDF's stored text (textcheck.py).
2. A table whose cells are mostly NOT in the stored text is probably a picture pasted into the PDF, which has no
   stored text. Such a table is re-read from the original picture: the picture is stored in the PDF at its own,
   often much higher, resolution than the shrunken page render the model first saw, and the model reads it far more
   accurately enlarged and on its own.
3. OCR (ocr.py) reads the same picture as an independent second reader. It referees between the page reading and the
   picture reading, and is the check for a table that has no stored text. A cell both readers produce is confirmed.
4. For every table with cells to check, the position of each affected row is recorded, so the workbook's Review sheet
   can show a crop of that row next to what was read.

Each table ends up with `verified_by`: "text layer", "OCR", or "none" (nothing could confirm it).
"""
from __future__ import annotations

import copy
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from PIL import Image

from . import ocr as ocr_module
from .parse import is_figure, parse_reply
from .pdfpages import find_text_boxes, page_images, page_size, render_pages
from .textcheck import MIN_TEXT, check_cells, normalize

REREAD_THRESHOLD = 0.25  # re-read a table when at least this share of its cells is not in the PDF's text
MIN_SIMILARITY = 0.3  # how alike the page reading and the picture reading must be to count as the same table
OCR_DPI, OCR_MAX_PIXELS = 300, 9_000_000  # full-page render used when OCR has to read a whole page


def unconfirmed_fraction(table: dict) -> float:
    """The share of the table's filled cells that the PDF's stored text does not confirm (1.0 with no stored text)."""
    cells = [c for row in table["rows"] for c in row if normalize(c)]
    if not cells:
        return 0.0
    if not table.get("text_checked"):
        return 1.0
    return len(table["not_in_text"]) / len(cells)


def similarity(a: dict, b: dict) -> float:
    """How alike two readings of (what may be) the same table are, by their cell text alone."""
    x = ocr_module.squash("".join("".join(row) for row in a["rows"]))
    y = ocr_module.squash("".join("".join(row) for row in b["rows"]))
    if not x or not y:
        return 0.0
    matcher = SequenceMatcher(None, x, y, autojunk=False)
    return matcher.ratio() if matcher.quick_ratio() >= MIN_SIMILARITY else 0.0


def verified_by(table: dict) -> str:
    if table.get("picture_table") or not table.get("text_checked"):
        return "OCR" if table.get("ocr_checked") else "none"
    return "text layer"


def flag_cells(table: dict) -> dict[tuple[int, int], str]:
    """The cells a person should check, with the reason. Empty when nothing is wrong or nothing could be checked."""
    kind = table.get("verified_by") or verified_by(table)
    flags: dict[tuple[int, int], str] = {}
    if kind == "OCR":
        for r, c in table.get("ocr_close", []):
            flags[(r, c)] = "OCR reads this with a few letters different"
        for r, c in table.get("ocr_missing", []):
            flags[(r, c)] = "OCR does not produce this value"
    elif table.get("text_checked"):
        for r, c in table.get("not_in_text", []):
            flags[(r, c)] = "not found in the PDF's text"
        for r, c, longer in table.get("clipped", []):
            flags[(r, c)] = f"only the start of a longer value stored in the PDF: {longer}"
    return flags


def verify_page(
    record: dict,
    *,
    pdf: Path,
    page: int,
    text: str,
    out_dir_page: Path,
    reader,
    ocr_function: Callable[[Image.Image], list] | None,
    reread: bool = True,
    threshold: float = REREAD_THRESHOLD,
    reread_max_pixels: int = 6_000_000,
) -> None:
    """Check, and where needed improve, the tables in record["tables"]. Writes picture files into out_dir_page."""
    tables = record["tables"]
    notes: list[str] = record.setdefault("verify_notes", [])
    for table in tables:
        check_cells(table, text)
        # a table on a page that HAS stored text, but whose cells are mostly not in it: a picture pasted into the page.
        # (A page with no stored text at all is a scan; that is "no text layer", a different case with the same fix.)
        table["picture_table"] = bool(table["text_checked"]) and unconfirmed_fraction(table) >= threshold
    flagged = [t for t in tables if unconfirmed_fraction(t) >= threshold]

    crops = _read_pictures(record, pdf, page, reader, reread_max_pixels, out_dir_page, notes) if flagged and reread else []
    if flagged and crops:
        _adopt_picture_readings(flagged, crops, ocr_function, notes)

    unchecked = [t for t in flagged if not t.get("ocr_checked")]
    if unchecked and ocr_function is not None:
        _ocr_whole_page(unchecked, pdf, page, ocr_function, out_dir_page, notes)

    for table in tables:
        table["verified_by"] = verified_by(table)
    _locate_rows(tables, pdf, page, text, notes)


def _read_pictures(record, pdf, page, reader, max_pixels, out_dir_page, notes) -> list[dict]:
    """Read each larger picture stored on the page on its own, enlarged. Returns one entry per picture read."""
    crops = []
    try:
        pictures = page_images(pdf, page, max_pixels)
    except Exception as exc:  # a damaged image object must not lose the page
        notes.append(f"could not extract the pictures on this page: {exc}")
        return crops
    for k, picture in enumerate(pictures, 1):
        name = f"p{page:04d}_img{k}.png"
        try:
            reply = reader.read(picture.image, "")  # no text hint: a picture has no stored text to give
            parsed = [asdict(t) for t in parse_reply(reply.text) if not is_figure(t)]
        except ValueError:
            notes.append(f"{name}: the model's reply for this picture was not usable")
            continue
        except Exception as exc:
            notes.append(f"{name}: {type(exc).__name__}: {exc}")
            reader.free_memory()
            continue
        record["reread_seconds"] = round(record.get("reread_seconds", 0) + reply.seconds, 1)
        if parsed:
            out_dir_page.mkdir(parents=True, exist_ok=True)
            picture.image.save(out_dir_page / name)
            crops.append({"name": name, "image": picture.image, "tables": parsed, "lines": None})
    return crops


def _adopt_picture_readings(flagged, crops, ocr_function, notes) -> None:
    """Pair each flagged page table with the picture reading most like it, and keep whichever reading OCR prefers."""
    candidates = sorted(
        ((similarity(t, ct), i, ci, cj) for i, t in enumerate(flagged)
         for ci, crop in enumerate(crops) for cj, ct in enumerate(crop["tables"])),
        reverse=True)
    used_page, used_crop = set(), set()
    for score, i, ci, cj in candidates:
        if score < MIN_SIMILARITY or i in used_page or (ci, cj) in used_crop:
            continue
        used_page.add(i)
        used_crop.add((ci, cj))
        page_table, crop = flagged[i], crops[ci]
        picture_table = copy.deepcopy(crop["tables"][cj])
        for field in ("label", "caption", "footnotes"):  # the caption sits outside the picture; keep the page's
            picture_table[field] = page_table[field] or picture_table[field]
        picture_table["continued"] = page_table["continued"]

        lines = _ocr_lines(crop, ocr_function, notes)
        page_copy = copy.deepcopy(page_table)
        score_page = score_picture = None
        if lines is not None:
            ocr_module.check_table(page_copy, lines)
            ocr_module.check_table(picture_table, lines)
            score_page, score_picture = ocr_module.agreement(page_copy), ocr_module.agreement(picture_table)
        take_picture = lines is None or score_picture >= score_page  # ties go to the higher-resolution reading

        chosen = picture_table if take_picture else page_copy
        meta = {"image": crop["name"], "chosen": "picture" if take_picture else "page", "similarity": round(score, 2),
                "page_agreement": None if score_page is None else round(score_page, 2),
                "picture_agreement": None if score_picture is None else round(score_picture, 2)}
        if take_picture:
            meta["page_rows"] = page_table["rows"]  # kept so the earlier reading can be inspected
        chosen.update(picture_table=page_table.get("picture_table", False), reread=meta, review_image=crop["name"],
                      text_checked=False, not_in_text=[], clipped=[])
        page_table.clear()
        page_table.update(chosen)


def _ocr_lines(crop: dict, ocr_function, notes):
    if ocr_function is None:
        return None
    if crop["lines"] is None:
        try:
            crop["lines"] = ocr_function(crop["image"])
        except Exception as exc:
            notes.append(f"OCR failed on {crop['name']}: {exc}")
            crop["lines"] = False  # remember the failure so it is not retried for every table
    return crop["lines"] or None


def _ocr_whole_page(tables, pdf, page, ocr_function, out_dir_page, notes) -> None:
    """OCR the whole page (at higher resolution than the model sees) for picture tables with no picture reading."""
    try:
        big = next(render_pages(pdf, [page], OCR_DPI, OCR_MAX_PIXELS))[1]
        lines = ocr_function(big)
    except Exception as exc:
        notes.append(f"OCR failed on the page: {exc}")
        return
    name = f"p{page:04d}_ocr.png"
    out_dir_page.mkdir(parents=True, exist_ok=True)
    big.save(out_dir_page / name)
    for table in tables:
        ocr_module.check_table(table, lines)
        table["review_image"] = name
        table["text_checked"], table["not_in_text"], table["clipped"] = False, [], []


def _locate_rows(tables, pdf, page, text, notes) -> None:
    """For tables checked against the stored text, find where each flagged row is on the page image p<page>.png."""
    wanted: dict[int, dict[int, list[str]]] = {}
    for t_index, table in enumerate(tables):
        if table["verified_by"] != "text layer":
            continue
        rows = {r for r, _ in flag_cells(table)}
        for r in rows:
            row = table["rows"][r]
            flagged_cols = {c for (rr, c) in flag_cells(table) if rr == r}
            probes = [cell.split("\n")[0].strip() for c, cell in enumerate(row)
                      if c not in flagged_cols and len(normalize(cell.split("\n")[0])) >= 4]
            if probes:
                wanted.setdefault(t_index, {})[r] = [probes[0], probes[-1]]
    if not wanted:
        return
    queries = sorted({q for rows in wanted.values() for probes in rows.values() for q in probes})
    try:
        boxes = find_text_boxes(pdf, page, queries)
        width, height = page_size(pdf, page)
    except Exception as exc:
        notes.append(f"could not locate rows on the page: {exc}")
        return
    for t_index, rows in wanted.items():
        located = {}
        for r, probes in rows.items():
            found = [boxes[q] for q in probes if q in boxes]
            if found:
                located[str(r)] = [min(b[0] for b in found), min(b[1] for b in found),
                                   max(b[2] for b in found), max(b[3] for b in found)]
        if located:
            # PDF points (origin bottom left) -> fractions of the page image (origin top left)
            tables[t_index]["row_boxes"] = {
                r: [b[0] / width, 1 - b[3] / height, b[2] / width, 1 - b[1] / height] for r, b in located.items()}
            tables[t_index]["row_boxes_are_fractions"] = True
            tables[t_index]["review_image"] = f"p{page:04d}.png"
