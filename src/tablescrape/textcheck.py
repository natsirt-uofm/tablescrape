"""Check the cells the model wrote against the PDF's own text layer.

A vision model reading an image makes typist's mistakes: a changed letter in a DNA sequence, a dropped digit. Where
the PDF stores its text, that text is exact, so every cell is looked up in it. A cell whose characters do not occur
anywhere in the page's text is marked; the workbook highlights it for a person to check against the page image.

A second kind of mistake the lookup would miss: a table clipped by the page edge (a web page printed to PDF cuts off
its right-hand column) shows only the start of a value, while the text layer holds all of it. The visible stub IS a
substring of the stored text, so it passes the first test. A cell that is the beginning of a longer word in the text
layer is therefore marked as "clipped", with the longer value, which the workbook shows as a cell comment.

The lookup ignores whitespace (the text layer breaks cells across lines, "AHT\\nH203Ren", and the model adds or drops
spaces) and treats look-alike characters as equal (ligatures, superscript digits, the different dashes).
"""
from __future__ import annotations

import re
import unicodedata

MIN_TEXT = 20  # fewer characters than this: the page is a picture, there is nothing to check against
MIN_CLIPPED = 10  # shorter cells ("AHT", "Adult") are a prefix of too many ordinary words to mean anything
_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\ufffe\x02\u200b"))  # soft hyphen, markers some PDFs store, zero-width space
_DASHES = str.maketrans({"\u2212": "-", "\u2013": "-", "\u2014": "-", "\u2010": "-", "\u2011": "-"})  # minus, en/em dash, hyphens


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_INVISIBLE).translate(_DASHES)
    return re.sub(r"\s+", "", text)


def check_cells(table: dict, page_text: str) -> None:
    """Set table["text_checked"], table["not_in_text"] (a list of [row, column] of body cells not found in the text)
    and table["clipped"] (a list of [row, column, longer value] of cells that are only the start of a longer word)."""
    if len(page_text.strip()) < MIN_TEXT:
        table["text_checked"], table["not_in_text"], table["clipped"] = False, [], []
        return
    haystack = normalize(page_text)
    words = {w for w in map(normalize, page_text.split()) if len(w) >= MIN_CLIPPED}
    missing, clipped = [], []
    for r, row in enumerate(table["rows"]):
        for c, cell in enumerate(row):
            needle = normalize(cell)
            if not needle:
                continue
            if needle not in haystack:
                missing.append([r, c])
            elif len(needle) >= MIN_CLIPPED:
                longer = [w for w in words if len(w) >= len(needle) + 3 and w.startswith(needle)]
                if longer:
                    clipped.append([r, c, max(longer, key=len)])
    table["text_checked"], table["not_in_text"], table["clipped"] = True, missing, clipped
