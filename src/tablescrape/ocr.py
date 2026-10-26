"""A second, independent reader for tables that have no stored text: OCR with Tesseract.

The stored-text check (textcheck.py) cannot help with a table pasted into a PDF as a picture: there is no text to look
cells up in. OCR reads the same pixels the model reads, but it is a different kind of reader that makes different
mistakes, so a cell that both the model and OCR produce is much less likely to be a misreading than a cell only the
model produced. A cell the two disagree on is flagged for a person.

OCR is not exact (it confuses T with I, drops letters), so a cell found with a few letters different is "close", not
"ok". Neither reader is trusted alone: ok means agreement, close and missing mean look at the page.

Tesseract is optional. Without it nothing here runs and picture tables stay marked as unverified.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable

from PIL import Image


@dataclass
class OcrLine:
    text: str
    box: tuple[int, int, int, int]  # left, top, right, bottom, in pixels of the image that was read


OcrFunction = Callable[[Image.Image], list[OcrLine]]


def tesseract_available() -> bool:
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:  # pytesseract not installed, or the tesseract program is not on PATH
        return False


def read_lines(image: Image.Image) -> list[OcrLine]:
    """Run Tesseract on the image and return its text lines with their positions (top to bottom)."""
    import pytesseract

    # --psm 6: "assume a single uniform block of text", which suits a table printed as lines of cells
    data = pytesseract.image_to_data(image.convert("L"), config="--psm 6", output_type=pytesseract.Output.DICT)
    words: dict[tuple[int, int, int], list[int]] = {}
    for i, text in enumerate(data["text"]):
        if not text.strip() or float(data["conf"][i]) < 0:
            continue
        words.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(i)
    lines = []
    for idx in words.values():
        idx.sort(key=lambda i: data["left"][i])
        lines.append(OcrLine(
            text=" ".join(data["text"][i] for i in idx),
            box=(min(data["left"][i] for i in idx), min(data["top"][i] for i in idx),
                 max(data["left"][i] + data["width"][i] for i in idx),
                 max(data["top"][i] + data["height"][i] for i in idx))))
    lines.sort(key=lambda line: (line.box[1], line.box[0]))
    return lines


def squash(text: str) -> str:
    """Letters and digits only, upper case: OCR and the model disagree about spaces, quotes and dashes."""
    return re.sub(r"[^0-9A-Z]", "", unicodedata.normalize("NFKC", text).upper())


def distance_in(needle: str, haystack: str, limit: int) -> int:
    """The fewest edits turning needle into any piece of haystack (Sellers' algorithm); limit + 1 if more than limit."""
    if needle in haystack:
        return 0
    if limit == 0 or not needle or not haystack:
        return limit + 1
    previous = list(range(len(needle) + 1))  # column for "haystack consumed so far = nothing"
    best = previous[-1]
    for ch in haystack:
        current = [0]  # a match may start anywhere in the haystack, so the empty needle prefix costs nothing
        for i, nc in enumerate(needle, 1):
            current.append(min(previous[i] + 1, current[i - 1] + 1, previous[i - 1] + (nc != ch)))
        best = min(best, current[-1])
        previous = current
    return best if best <= limit else limit + 1


def _allowed_errors(length: int) -> int:
    """About one wrong letter in eight; none for very short cells."""
    return 0 if length < 5 else max(1, length // 8)


def check_table(table: dict, lines: list[OcrLine]) -> None:
    """Compare the table's cells with what OCR read. Sets, on the table:

    ocr_checked     True
    ocr_ok          number of cells OCR agrees with exactly
    ocr_close       [[row, col], ...] cells OCR reads with a few letters different
    ocr_missing     [[row, col], ...] cells OCR does not produce at all
    ocr_row_text    {row: the OCR text of the line(s) matched to that row}
    row_boxes       {row: [left, top, right, bottom]} where that row is, in pixels of the OCR image
    """
    squashed = [squash(line.text) for line in lines]
    everything = "".join(squashed)
    ok, close, missing = 0, [], []
    row_text: dict[str, str] = {}
    row_boxes: dict[str, list[int]] = {}
    cursor = 0  # rows are in page order, so each row is looked for from the previous match onwards
    for r, row in enumerate(table["rows"]):
        wanted = squash("".join(row))
        best, best_score = None, 0.0
        for start in range(cursor, min(cursor + 12, len(lines))):
            for span in (1, 2, 3):  # a row whose cells wrap onto extra lines is several OCR lines
                if start + span > len(lines):
                    break
                candidate = "".join(squashed[start:start + span])
                matcher = SequenceMatcher(None, wanted, candidate, autojunk=False)
                if wanted and candidate and matcher.quick_ratio() >= 0.5 and matcher.ratio() > best_score:
                    best, best_score = (start, span), matcher.ratio()
        if best is not None and best_score >= 0.5:
            start, span = best
            cursor = start + span
            haystack = "".join(squashed[start:start + span])
            chosen = lines[start:start + span]
            row_text[str(r)] = " | ".join(line.text for line in chosen)
            row_boxes[str(r)] = [min(line.box[0] for line in chosen), min(line.box[1] for line in chosen),
                                 max(line.box[2] for line in chosen), max(line.box[3] for line in chosen)]
        else:
            haystack = everything  # row not located: all that can be said is whether OCR produced the value anywhere
        for c, cell in enumerate(row):
            needle = squash(cell)
            if len(needle) < 2:
                continue
            d = distance_in(needle, haystack, _allowed_errors(len(needle)))
            if d == 0:
                ok += 1
            elif d <= _allowed_errors(len(needle)):
                close.append([r, c])
            else:
                missing.append([r, c])
    table.update(ocr_checked=True, ocr_ok=ok, ocr_close=close, ocr_missing=missing,
                 ocr_row_text=row_text, row_boxes=row_boxes)


def agreement(table: dict) -> float:
    """The share of the table's checked cells that OCR agrees with exactly (1.0 when there were none to check)."""
    total = table["ocr_ok"] + len(table["ocr_close"]) + len(table["ocr_missing"])
    return table["ocr_ok"] / total if total else 1.0
