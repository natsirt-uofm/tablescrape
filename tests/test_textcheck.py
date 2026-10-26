from tablescrape.parse import Table, is_figure
from tablescrape.textcheck import check_cells
from tablescrape.vlm import PROMPT, build_prompt

# the text layer of Breen et al. 2001, Table 2, page 3 (cells broken across lines, as the PDF stores them)
PAGE = """AHT
H203Ren
CTTTACCTGCTTATGCTCACTCTCG TCTTCCTGTGGGAGTTGAGAACAT
AHT
H127
ATCGACTTGTTCTTTGTCTCAGC TTTGGATATGATGGGGAATTTA
Fst 0.12 \u2212 0.05 \ufb01xed"""


def test_cells_the_text_layer_confirms_are_not_flagged():
    table = {"rows": [["AHT H203Ren", "CTTTACCTGCTTATGCTCACTCTCG"], ["AHT H127", "ATCGACTTGTTCTTTGTCTCAGC"]]}
    check_cells(table, PAGE)
    assert table["text_checked"] and table["not_in_text"] == []


def test_misread_cells_are_flagged():
    table = {"rows": [["AHT H127", "ATCGACTTGTTCCTTGTCTCAGC"],   # one letter changed
                      ["AHT H239", "TGT T T CATTTGGTCTGCGTGT"]]}  # not on this page at all
    check_cells(table, PAGE)
    assert table["not_in_text"] == [[0, 1], [1, 0], [1, 1]]


def test_look_alike_characters_count_as_equal():
    table = {"rows": [["-0.05", "fixed", ""]]}  # hyphen vs the PDF's minus sign, "fi" vs its ligature; empty cell
    check_cells(table, PAGE)
    assert table["not_in_text"] == []


def test_picture_only_page_is_not_checked():
    table = {"rows": [["x"]]}
    check_cells(table, "  3 ")
    assert table == {"rows": [["x"]], "text_checked": False, "not_in_text": [], "clipped": []}


def test_prompt_carries_the_text_layer_only_when_there_is_one():
    assert build_prompt("") == PROMPT
    assert build_prompt(PAGE).startswith(PROMPT) and "TCTTCCTGTGGGAGTTGAGAACAT" in build_prompt(PAGE)


def test_figure_labels():
    def t(label="", caption=""):
        return Table(columns=["a"], rows=[["1"]], label=label, caption=caption)
    assert is_figure(t("Fig. 4")) and is_figure(t(caption="Figure S2. Tree")) and is_figure(t("FIG 1"))
    assert not is_figure(t("Table 1")) and not is_figure(t(caption="Figures of merit for each locus"))


def test_cell_clipped_by_the_page_edge_is_flagged_with_the_full_value():
    # the page shows only "TCTTCCTGTGGGAGTTGA"; the PDF stores "...GAGAACAT"
    table = {"rows": [["AHT H203Ren", "CTTTACCTGCTTATGCTCACTCTCG", "TCTTCCTGTGGGAGTTGA"]]}
    check_cells(table, PAGE)
    assert table["not_in_text"] == []
    assert table["clipped"] == [[0, 2, "TCTTCCTGTGGGAGTTGAGAACAT"]]


def test_complete_and_short_cells_are_not_called_clipped():
    table = {"rows": [["AHT", "TCTTCCTGTGGGAGTTGAGAACAT", "TCTTCCTG"]]}  # whole value; and a short stub (< 10 characters)
    check_cells(table, PAGE)
    assert table["clipped"] == []
