"""The verification stage on generated PDFs: re-reading a picture table, OCR as referee, and the Review sheet.

No GPU and no Tesseract are needed: the model and the OCR engine are replaced by scripted stand-ins.
"""
import json

import pypdfium2 as pdfium
import pytest
from openpyxl import load_workbook
from PIL import Image, ImageDraw

from tablescrape import ocr
from tablescrape.export import build
from tablescrape.ocr import OcrLine
from tablescrape.pipeline import extract
from tablescrape.vlm import Reply

GOOD = [["UarMU05", "GTGATTTTTCTTGTAGCCTAGG"], ["UarMU09", "AGCCACTTTGTAAGGAGTAGT"], ["UarMU10", "TTCAGATTTCATCAGTTTGAC"]]
BAD = [["UarMU05", "GTGATTTTTCCTTGTAGCTAGG"], ["UarMU09", "AGCCACTTTGTAAGGAGTAGT"], ["UarMU10", "TTCAGATTGATCAGTTTGAC"]]


def reply(rows, label="Table 1", caption="Primers"):
    return Reply(text=json.dumps({"tables": [{"label": label, "caption": caption, "columns": ["Locus", "Primer"],
                                              "rows": rows, "footnotes": "", "continued": False}]}),
                 truncated=False, seconds=0.0)


class ScriptedReader:
    """Answers the first read (the whole page) with BAD and the second (the picture alone) with GOOD."""
    name = "scripted"

    def __init__(self, page_rows, picture_rows):
        self.answers = [reply(page_rows), reply(picture_rows)]
        self.seen = []

    def read(self, image, page_text=""):
        self.seen.append((image.size, page_text))
        return self.answers[min(len(self.seen) - 1, len(self.answers) - 1)]

    def free_memory(self):
        pass


def picture_line(text, y):  # an OCR line as Tesseract would report it for the picture
    return OcrLine(text=text, box=(10, y, 900, y + 30))


def fake_ocr(image):
    return [picture_line("Locus Primer", 5), picture_line("UarMU05 GTGATTTTTCTTGTAGCCTAGG", 60),
            picture_line("UarMU09 AGCCACTTTGTAAGGAGTAGT", 120), picture_line("UarMU10 TTCAGATTTCATCAGTTTGAC", 180)]


def make_pdf_with_picture(path, with_text=True):
    """One page: a caption as real text, and the table as an embedded picture."""
    doc = pdfium.PdfDocument.new()
    page = doc.new_page(612, 792)
    picture = Image.new("RGB", (1500, 700), "white")
    ImageDraw.Draw(picture).text((20, 20), "UarMU05  GTGATTTTTCTTGTAGCCTAGG", fill="black")
    obj = pdfium.PdfImage.new(doc)
    obj.set_bitmap(pdfium.PdfBitmap.from_pil(picture))
    obj.set_matrix(pdfium.PdfMatrix().scale(450, 210).translate(80, 400))
    page.insert_obj(obj)
    page.gen_content()
    doc.save(str(path))


@pytest.fixture
def pdfs(tmp_path):
    d = tmp_path / "pdfs"
    d.mkdir()
    make_pdf_with_picture(d / "tab.pdf")
    return d


def run(pdfs, tmp_path, reader, **kw):
    out = tmp_path / "out"
    extract(pdfs, out, reader, dpi=50, log=lambda m: None, **kw)
    return out, json.loads((out / "pages/tab/p0001.json").read_text())


def test_picture_table_is_reread_from_the_picture_and_ocr_confirms_it(pdfs, tmp_path):
    reader = ScriptedReader(BAD, GOOD)
    out, rec = run(pdfs, tmp_path, reader, ocr=fake_ocr)
    [table] = rec["tables"]
    assert table["rows"] == GOOD                              # the picture reading replaced the shrunken-page one
    assert table["reread"]["chosen"] == "picture" and table["reread"]["page_rows"] == BAD
    assert table["reread"]["picture_agreement"] == 1.0 > table["reread"]["page_agreement"]
    assert table["label"] == "Table 1" and table["verified_by"] == "OCR"
    assert table["ocr_close"] == [] and table["ocr_missing"] == []
    assert (out / "pages/tab/p0001_img1.png").exists()
    assert len(reader.seen) == 2 and reader.seen[1][1] == ""  # the picture is read with no text hint
    assert reader.seen[1][0][0] > 1000                         # ... and at its own, much larger, size


def test_ocr_keeps_the_page_reading_when_it_agrees_better(pdfs, tmp_path):
    out, rec = run(pdfs, tmp_path, ScriptedReader(GOOD, BAD), ocr=fake_ocr)  # now the picture reading is the worse one
    [table] = rec["tables"]
    assert table["rows"] == GOOD and table["reread"]["chosen"] == "page"


def test_without_ocr_the_picture_reading_is_taken_and_marked_unverified(pdfs, tmp_path):
    out, rec = run(pdfs, tmp_path, ScriptedReader(BAD, GOOD), ocr=False)
    [table] = rec["tables"]
    assert table["rows"] == GOOD and table["verified_by"] == "none" and "ocr_checked" not in table


def test_reread_can_be_switched_off(pdfs, tmp_path):
    reader = ScriptedReader(BAD, GOOD)
    out, rec = run(pdfs, tmp_path, reader, ocr=fake_ocr, reread=False)
    [table] = rec["tables"]
    assert len(reader.seen) == 1 and table["rows"] == BAD and "reread" not in table
    assert table["verified_by"] == "OCR"  # still checked by OCR, from the whole page this time


def test_ocr_flags_the_cells_it_disagrees_with(pdfs, tmp_path):
    out, rec = run(pdfs, tmp_path, ScriptedReader(BAD, BAD), ocr=fake_ocr)
    [table] = rec["tables"]
    assert table["rows"] == BAD
    flagged = {tuple(x) for x in table["ocr_close"]} | {tuple(x) for x in table["ocr_missing"]}
    assert flagged == {(0, 1), (2, 1)}          # the two misread primers, not the correct one in row 1
    assert table["row_boxes"]["0"][1] == 60      # row 0 was matched to the OCR line at y=60


def test_review_sheet_has_a_crop_for_each_flagged_row(pdfs, tmp_path):
    out, rec = run(pdfs, tmp_path, ScriptedReader(BAD, BAD), ocr=fake_ocr)
    build(out)
    wb = load_workbook(out / "tables.xlsx")
    assert wb.sheetnames[:3] == ["Index", "Documents", "Review"]
    ws = wb["Review"]
    assert [c.value for c in ws[1]][:4] == ["table", "page", "row", "cells to check"]
    rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if r[0]]
    assert [r[2] for r in rows] == [1, 3]                       # rows 1 and 3 of the table, counted from 1
    assert "GTGATTTTTCCTTGTAGCTAGG" in rows[0][3] and "UarMU05 GTGATTTTTCTTGTAGCCTAGG" in rows[0][5]
    assert len(ws._images) == 2                                 # one embedded crop per flagged row
    index = wb["Index"]
    header = [c.value for c in index[1]]
    values = dict(zip(header, [c.value for c in index[2]]))
    assert values["verified_by"] == "OCR" and values["cells_to_check"] == "2"


def test_no_review_sheet_when_nothing_is_flagged(pdfs, tmp_path):
    out, rec = run(pdfs, tmp_path, ScriptedReader(GOOD, GOOD), ocr=fake_ocr)
    build(out)
    assert "Review" not in load_workbook(out / "tables.xlsx").sheetnames


class TestOcrMatching:
    def lines(self, *texts):
        return [OcrLine(t, (0, i * 40, 500, i * 40 + 30)) for i, t in enumerate(texts)]

    def test_cell_agreement_levels(self):
        table = {"rows": [["Locus A", "GTGATTTTTCTTGTAGCCTAGG", "12"]]}
        ocr.check_table(table, self.lines("Locus A GTGATTTTTCTTGTAGCCTAGG 12"))
        assert table["ocr_ok"] == 3 and table["ocr_close"] == [] and table["ocr_missing"] == []

    def test_one_wrong_letter_is_close_not_ok(self):
        table = {"rows": [["Locus A", "GTGATTTTTCTTGTAGCCTAGG"]]}
        ocr.check_table(table, self.lines("Locus A GTGATTTTTCTTGTAGCCTAAG"))
        assert table["ocr_close"] == [[0, 1]] and table["ocr_missing"] == []

    def test_a_value_ocr_never_produced_is_missing(self):
        table = {"rows": [["Locus A", "GTGATTTTTCTTGTAGCCTAGG"]]}
        ocr.check_table(table, self.lines("Locus A", "something else entirely"))
        assert table["ocr_missing"] == [[0, 1]]

    def test_rows_are_matched_in_order_even_when_a_value_repeats(self):
        table = {"rows": [["a", "ACGTACGTACGT"], ["b", "ACGTACGTACGT"]]}
        ocr.check_table(table, self.lines("a ACGTACGTACGT", "b ACGTACGTACGT"))
        assert table["row_boxes"]["0"][1] == 0 and table["row_boxes"]["1"][1] == 40

    def test_a_row_wrapped_over_two_ocr_lines_is_still_matched(self):
        table = {"rows": [["AHT\nH203Ren", "CTTTACCTGCTTATGCTCACTCTCG"]]}
        ocr.check_table(table, self.lines("AHT", "H203Ren CTTTACCTGCTTATGCTCACTCTCG"))
        assert table["ocr_missing"] == [] and table["row_boxes"]["0"][3] == 70

    def test_distance_in(self):
        assert ocr.distance_in("ACGT", "TTACGTTT", 2) == 0
        assert ocr.distance_in("ACGT", "TTACCTTT", 2) == 1
        assert ocr.distance_in("ACGT", "TTTTTTTT", 1) == 2
