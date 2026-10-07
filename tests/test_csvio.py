"""Offline CSV-v2 regressions from supplied interfaces [26]. Results unknown."""

import csv
import io
from pathlib import Path

import pytest

from massing.csvio import (
    COLUMNS,
    CsvValueError,
    parse_csv_bytes,
    parse_csv_text,
    parse_row,
    parse_source_url,
    read_csv,
    roundtrip_csv,
    roundtrip_preserved,
)

HEADERS = ("name", "location", "height", "building_count", "floor_area", "status", "source_pdf")
SAMPLE = ["Synthetic building", " Synthetic location ", " 24 meters ", "1", "",
          " In Service ", "test-only://not-city-evidence"]
EXAMPLES = Path(__file__).resolve().parents[1] / "examples/csv2massing/synthetic"


def document(rows, header=HEADERS):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def test_exact_header_and_raw_cells_with_rfc_quoting_and_physical_lines():
    first = [*SAMPLE]
    first[0], first[5] = 'Synthetic "A",\nNorth', 'Status "quoted"\nsecond line'
    result = parse_csv_text(document([first, SAMPLE]))
    assert COLUMNS == HEADERS and result.header == HEADERS
    assert result.file_ok and result.ok and not result.findings
    assert [(r.row_number, r.line) for r in result.rows] == [(2, 4), (3, 5)]
    assert result.rows[0].raw == tuple(first)
    assert result.rows[0].name == first[0] and result.rows[0].status == first[5]
    assert result.rows[1].location == SAMPLE[1]
    assert result.rows[1].height.raw == SAMPLE[2]
    assert result.valid_rows == result.rows and not result.rejected_rows


@pytest.mark.parametrize("header", [HEADERS[:-1], (*HEADERS, "extra"),
                                    (*HEADERS[:-1], "name"), tuple(reversed(HEADERS))])
def test_missing_extra_duplicate_or_reordered_headers_are_file_errors(header):
    result = parse_csv_text(document([SAMPLE], header))
    assert result.header == header and not result.file_ok and not result.ok
    assert "header_mismatch" in {f.code for f in result.file_findings}
    assert len(result.rows) == 1 and result.rows[0].raw == tuple(SAMPLE)
    assert not result.valid_rows and result.rejected_rows == result.rows


@pytest.mark.parametrize("cells,code", [([], "blank_row"), (SAMPLE[:-1], "wrong_cell_count"),
                                       ([*SAMPLE, "extra"], "wrong_cell_count")])
def test_wrong_cell_counts_retained_with_row_details(cells, code):
    result = parse_csv_text(document([cells]))
    row, = result.rows
    assert result.file_ok and not result.ok and not row.valid
    assert row.raw == tuple(cells) and row.row_number == 2 and row.line == 2
    assert row.findings[0].code == code and row.findings[0].message


def test_group_unknown_and_result_properties_without_expanding_rows():
    group = [*SAMPLE]
    group[2:5] = [" UNKNOWN ", "2", ""]
    bad = [*SAMPLE]
    bad[2] = "24"
    result = parse_csv_text(document([SAMPLE, group, bad]))
    assert result.file_ok and not result.ok and len(result.rows) == 3
    assert result.valid_rows == result.rows[:2] and result.rejected_rows == result.rows[2:]
    assert result.unknown_height_rows == (result.rows[1],)
    row = result.rows[1]
    assert row.valid and not row.height_known and row.building_count == 2
    assert row.floor_area_m2 is None and row.height.height_m is None
    assert result.rows[0].height_known and result.rows[0].source_pdf_test_only is True
    assert result.findings == result.rows[2].findings
    issue, = result.findings
    assert (issue.code, issue.column, issue.raw, issue.row_number) == ("unitless_height", "height", "24", 4)


@pytest.mark.parametrize("column", [0, 1, 5])
def test_required_text_fields_are_not_silently_filled(column):
    cells = [*SAMPLE]
    cells[column] = " "
    row = parse_row(cells, 7, 12)
    assert not row.valid and row.raw == tuple(cells)
    assert any(f.code == "missing_value" and f.column == HEADERS[column]
               and f.row_number == 7 and f.line == 12 for f in row.findings)


def test_bom_utf8_empty_header_only_and_read_errors(tmp_path):
    result = parse_csv_bytes(("\ufeff" + document([SAMPLE])).encode())
    assert result.ok and result.file_ok and result.rows[0].raw == tuple(SAMPLE)
    assert [(f.code, f.severity) for f in result.file_findings] == [("bom_removed", "note")]
    for data, code in [(b"\xff", "not_utf8"), (b"", "missing_header"),
                       (document([]).encode(), "empty_data")]:
        parsed = parse_csv_bytes(data)
        assert not parsed.file_ok and not parsed.ok and not parsed.rows
        assert code in {f.code for f in parsed.file_findings}
    for path in (tmp_path / "absent.csv", tmp_path):
        parsed = read_csv(path)
        assert not parsed.file_ok and parsed.file_findings[0].code == "unreadable_file"
    path = tmp_path / "valid.csv"
    path.write_bytes(document([SAMPLE]).encode())
    assert read_csv(path).rows == parse_csv_text(document([SAMPLE])).rows


def test_malformed_quotes_are_fatal_but_prior_raw_rows_survive():
    result = parse_csv_text(document([SAMPLE]) + '"unterminated')
    assert not result.file_ok and not result.ok
    assert result.rows[0].raw == tuple(SAMPLE) and result.rows[0].valid
    assert any(f.code == "malformed_csv" and f.line == 3 for f in result.file_findings)


def test_roundtrip_raw_cells_and_explicit_omissions():
    result = parse_csv_text(document([SAMPLE, SAMPLE[:-1], [], [*SAMPLE[:2], "unknown", *SAMPLE[3:]]]))
    returned = roundtrip_csv(result)
    assert returned.written_rows == (2, 5) and returned.omitted_rows == (3, 4)
    assert returned.text.startswith(",".join(HEADERS) + "\r\n")
    assert roundtrip_preserved(result, returned.text)
    assert not roundtrip_preserved(result, returned.text.replace("24 meters", "25 meters", 1))
    assert not roundtrip_preserved(result, returned.text + '"unterminated')
    assert not roundtrip_preserved(result, returned.text.replace("name,location", "location,name", 1))
    assert [r.raw for r in parse_csv_text(returned.text).rows] == [result.rows[0].raw, result.rows[3].raw]


def test_checked_in_ten_rows_and_bad_first_height_are_not_optional():
    good = parse_csv_bytes((EXAMPLES / "buildings.csv").read_bytes())
    assert good.ok and len(good.rows) == 10 and all(r.height_known for r in good.rows)
    assert any(r.height.unit == "m" and r.height.height_m == 24 for r in good.rows)
    assert any(r.height.storeys == 10 and r.height.height_m == 32.5 for r in good.rows)
    returned = roundtrip_csv(good)
    assert returned.written_rows == tuple(range(2, 12)) and not returned.omitted_rows
    assert roundtrip_preserved(good, returned.text)
    bad = parse_csv_bytes((EXAMPLES / "bad.csv").read_bytes())
    assert bad.file_ok and not bad.ok and bad.rows[0].raw[2] == "24"
    assert bad.rejected_rows == (bad.rows[0],) and bad.rows[0].row_number == 2
    assert [f.code for f in bad.findings] == ["unitless_height"]
    assert [r.raw for r in bad.rows[1:]] == [r.raw for r in good.rows[1:]]
    assert bad.rows[0].raw[:2] == good.rows[0].raw[:2] and bad.rows[0].raw[3:] == good.rows[0].raw[3:]


@pytest.mark.parametrize("url", ["https://:443/report.pdf", "https://user@/report.pdf",
                                 "https://example.org:bad/report.pdf", "https://example.org:65536/a"])
def test_invalid_http_authority_is_plain_value_error_and_row_finding(url):
    with pytest.raises(CsvValueError):
        parse_source_url(url)
    cells = [*SAMPLE[:-1], url]
    row = parse_row(cells, 2, 2)
    assert not row.valid and row.findings[0].code == "invalid_source_pdf"
