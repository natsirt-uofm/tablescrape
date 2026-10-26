import pytest

from tablescrape.parse import parse_reply


def test_plain_reply():
    [t] = parse_reply('{"tables": [{"label": "Table 1", "caption": "Sizes", "columns": ["a", "b"], '
                      '"rows": [["1", "2"], ["3", "4"]], "footnotes": "", "continued": false}]}')
    assert (t.label, t.caption, t.columns, t.rows, t.warnings) == ("Table 1", "Sizes", ["a", "b"], [["1", "2"], ["3", "4"]], [])


def test_no_tables():
    assert parse_reply('{"tables": []}') == []
    assert parse_reply('```json\n{}\n```') == []


def test_code_fence_and_bare_list():
    [t] = parse_reply('```json\n[{"columns": ["x"], "rows": [["1"]]}]\n```')
    assert t.rows == [["1"]]


def test_unquoted_numbers_keep_their_printed_text():
    [t] = parse_reply('{"tables": [{"columns": ["v"], "rows": [[1.10], [0.050], [12]]}]}')
    assert t.rows == [["1.10"], ["0.050"], ["12"]]


def test_reply_cut_off_mid_table_keeps_the_complete_rows():
    [t] = parse_reply('```json\n{"tables": [{"label": "Table 2", "columns": ["a", "b"], "rows": [["1", "2"], ["3", "4"], ["5"')
    assert t.rows[:2] == [["1", "2"], ["3", "4"]]
    assert t.label == "Table 2"


def test_ragged_rows_are_padded_and_flagged():
    [t] = parse_reply('{"tables": [{"columns": ["a", "b", "c"], "rows": [["1", "2", "3"], ["4"]]}]}')
    assert t.rows[1] == ["4", "", ""]
    assert "padded" in t.warnings[0]


def test_short_header_gets_column_names():
    [t] = parse_reply('{"tables": [{"columns": ["a"], "rows": [["1", "2"]]}]}')
    assert t.columns == ["a", "column_2"]


def test_rows_written_as_objects():
    [t] = parse_reply('{"tables": [{"columns": ["a", "b"], "rows": [{"b": "2", "a": "1"}]}]}')
    assert t.rows == [["1", "2"]]


def test_empty_rows_and_empty_tables_are_dropped():
    assert parse_reply('{"tables": [{"columns": [], "rows": [["", ""]]}]}') == []


def test_not_json():
    with pytest.raises(ValueError):
        parse_reply("I could not find any tables on this page.")


def test_table_that_is_all_header_is_flagged():
    [t] = parse_reply('{"tables": [{"columns": ["a", "b", "c"], "rows": []}]}')
    assert t.rows == [] and "no data rows" in t.warnings[0]


def test_blank_header_names_become_column_n():
    [t] = parse_reply('{"tables": [{"columns": ["", "b", ""], "rows": [["1", "2", "3"]]}]}')
    assert t.columns == ["column_1", "b", "column_3"]
