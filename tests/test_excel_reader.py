from pathlib import Path

import pytest
from openpyxl import Workbook

from batch.excel import ExcelReaderError, read_excel_rows


def _build_workbook(path: Path, headers: list[str], rows: list[list[object]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_read_excel_rows_skips_empty_text_and_trims(tmp_path: Path) -> None:
    excel = tmp_path / "input.xlsx"
    _build_workbook(
        excel,
        ["text", "emotion", "activity_name", "audio_type"],
        [
            ["  hello  ", " happy ", " ad_1 ", " promo "],
            ["   ", "sad", "ad_2", "news"],
            ["world", None, None, "story"],
        ],
    )

    rows = read_excel_rows(excel)

    assert len(rows) == 2
    assert rows[0].row_index == 2
    assert rows[0].text == "hello"
    assert rows[0].emotion == "happy"
    assert rows[0].activity_name == "ad_1"
    assert rows[0].audio_type == "promo"
    assert rows[1].row_index == 4
    assert rows[1].text == "world"


def test_read_excel_rows_rejects_invalid_headers(tmp_path: Path) -> None:
    excel = tmp_path / "bad.xlsx"
    _build_workbook(
        excel,
        ["text", "activity_name", "emotion", "audio_type"],
        [["hello", "activity", "happy", "promo"]],
    )

    with pytest.raises(ExcelReaderError, match="Invalid headers"):
        read_excel_rows(excel)
