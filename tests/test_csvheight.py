"""Offline regression tests for CSV-v2 height, count, area and source URL parsers."""

import json
import math
from dataclasses import asdict

import pytest

import massing.csvio as csvio
from massing.csvio import (
    CsvValueError,
    parse_building_count,
    parse_floor_area,
    parse_height,
    parse_source_url,
    storeys_to_metres,
)


@pytest.mark.parametrize("raw,expected", [
    ("24 m", 24), ("24 metres", 24), ("24 meters", 24),
    ("  24 METRES  ", 24), ("24m", 24), ("00024 meters", 24),
    ("24.125 m", 24.125), ("0.001 m", .001),
])
def test_explicit_metres_unchanged_and_raw_preserved(raw, expected):
    value = parse_height(raw)
    assert value.raw == raw and value.known is True
    assert value.height_m == expected
    assert value.unit == "m" and value.storeys is None
    assert isinstance(value.method, str) and value.method
    assert math.isfinite(value.height_m)


@pytest.mark.parametrize("raw,count,expected", [
    ("10 storeys", 10, 32.5), ("10 stories", 10, 32.5),
    ("1 storey", 1, 5.5), ("1 story", 1, 5.5),
    ("  0010 STOREYS  ", 10, 32.5), ("2storeys", 2, 8.5),
    ("61 storeys", 61, 185.5),
])
def test_storey_aliases_formula_and_no_outlier_correction(raw, count, expected):
    value = parse_height(raw)
    assert value.raw == raw and value.known is True
    assert (value.height_m, value.unit, value.storeys) == (expected, "storeys", count)
    assert "schematic" in value.method.lower()
    assert storeys_to_metres(count) == expected


@pytest.mark.parametrize("raw", ["", " ", "\t", "unknown", " UNKNOWN ", "\tUnknown\t"])
def test_unknown_is_null_never_four_metre_placeholder(raw):
    value = parse_height(raw)
    assert value.raw == raw and value.known is False
    assert value.height_m is None and value.unit is None and value.storeys is None
    assert isinstance(value.method, str) and value.method
    assert "unknown" in value.method.lower()


def test_metres_and_unknown_ignore_storey_multiplier(monkeypatch):
    monkeypatch.setattr(csvio, "TYPICAL_STOREY_M", 99.0)
    for raw in ("24 m", "24 metres", "24 meters"):
        value = parse_height(raw)
        assert value.height_m == 24 and value.unit == "m" and value.storeys is None
    assert parse_height("unknown").height_m is None
    assert parse_height("").height_m is None


@pytest.mark.parametrize("raw", [" 24 meters ", " 10 stories ", " UNKNOWN "])
def test_height_dataclass_metadata_is_json_serializable(raw):
    value = parse_height(raw)
    restored = json.loads(json.dumps(asdict(value), allow_nan=False))
    assert restored == {
        "raw": raw, "known": value.known, "height_m": value.height_m,
        "unit": value.unit, "storeys": value.storeys, "method": value.method,
    }
    if not value.known:
        assert restored["height_m"] is None and restored["storeys"] is None


@pytest.mark.parametrize("raw", [
    "24", "24.5", "+24", "0 m", "-1 m", "0 storeys", "-3 storeys",
    "10.0 storeys", "2.5 stories", "+10 storeys", "3-7 storeys",
    "NaN m", "inf metres", "-Infinity meters", "NaN storeys",
    "1e2 m", "24 ft", "24 cm", "24 metre", "unknown m",
    "24 m trailing", "24 m; 10 storeys", "9" * 400 + " m",
])
def test_invalid_height_is_csv_value_error(raw):
    with pytest.raises(CsvValueError) as caught:
        parse_height(raw)
    assert isinstance(caught.value.code, str) and caught.value.code
    assert str(caught.value)


@pytest.mark.parametrize("digits", [16, 400, 4400])
def test_oversized_storey_text_has_no_integer_limit_or_overflow_leak(digits):
    with pytest.raises(CsvValueError) as caught:
        parse_height("9" * digits + " storeys")
    assert caught.value.code == "invalid_height"


@pytest.mark.parametrize("count", [
    pytest.param(10**15, id="sixteen-digits"),
    pytest.param(10**399, id="four-hundred-digits"),
    pytest.param(10**4399, id="four-thousand-four-hundred-digits"),
])
def test_oversized_integer_storeys_have_no_overflow_leak(count):
    with pytest.raises(CsvValueError) as caught:
        storeys_to_metres(count)
    assert caught.value.code == "invalid_height"


@pytest.mark.parametrize("count", [0, -1, True, False, 10.0, "10", None])
def test_storey_conversion_requires_positive_integer(count):
    with pytest.raises(CsvValueError) as caught:
        storeys_to_metres(count)
    assert caught.value.code == "invalid_height"


def test_fifteen_significant_storey_digits_are_supported():
    value = parse_height("999999999999999 storeys")
    assert value.storeys == 999999999999999 and value.known
    assert value.height_m > 0 and math.isfinite(value.height_m)


@pytest.mark.parametrize("raw,expected", [
    ("1", 1), (" 2 ", 2), ("0003", 3), ("999999999999999", 999999999999999),
])
def test_building_count_positive_whole_digits(raw, expected):
    result = parse_building_count(raw)
    assert type(result) is int and result == expected


@pytest.mark.parametrize("raw", [
    "", "unknown", "0", "000", "-1", "+2", "2.0", "2.5", "1e2",
    "two", "2 buildings", "1,000", "True",
    "9" * 16, "9" * 400, "9" * 4400,
])
def test_invalid_building_count_is_csv_value_error(raw):
    with pytest.raises(CsvValueError) as caught:
        parse_building_count(raw)
    assert caught.value.code == "invalid_building_count"


@pytest.mark.parametrize("raw,expected", [
    ("0", 0), ("0 m2", 0), ("1200", 1200),
    (" 1200.5 M2 ", 1200.5), ("00024.125", 24.125),
])
def test_group_floor_area_retained_without_allocation(raw, expected):
    area = parse_floor_area(raw)
    assert area == expected and math.isfinite(area)
    assert parse_building_count("2") == 2
    assert parse_floor_area(raw) == expected


@pytest.mark.parametrize("raw", ["", " ", "unknown", " UNKNOWN "])
def test_unknown_floor_area_is_null(raw):
    assert parse_floor_area(raw) is None


@pytest.mark.parametrize("raw", [
    "-1", "-0.01 m2", "NaN", "inf", "Infinity m2", "12 sqft",
    "12 m", "1,200", "12 m2 trailing", "9" * 400,
])
def test_invalid_floor_area_is_csv_value_error(raw):
    with pytest.raises(CsvValueError) as caught:
        parse_floor_area(raw)
    assert caught.value.code == "invalid_floor_area"


@pytest.mark.parametrize("raw,test_only", [
    ("https://example.org/report.pdf", False),
    ("http://example.org/report.pdf?page=3#evidence", False),
    ("HTTPS://example.org/report.pdf", False),
    ("test-only://not-city-evidence", True),
    ("test-only://synthetic/report.pdf?page=1", True),
])
def test_source_url_preserved_with_boolean_test_only_flag(raw, test_only):
    url, flag = parse_source_url(raw)
    assert url == raw and type(flag) is bool and flag is test_only


@pytest.mark.parametrize("raw", [
    "", "unknown", "report.pdf", "ftp://example.org/report.pdf",
    "https://", "https:///report.pdf", "https://[broken", "test-only://",
    " https://example.org/report.pdf", "https://example.org/report.pdf ",
    "https://example.org/a b.pdf", "https://example.org/\tfile.pdf",
    "https://example.org/report.pdf\n",
])
def test_malformed_source_url_is_csv_value_error(raw):
    with pytest.raises(CsvValueError) as caught:
        parse_source_url(raw)
    assert caught.value.code == "invalid_source_pdf" and str(caught.value)
