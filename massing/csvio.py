"""City CSV contract (SPEC v2.0): seven exact columns, UTF-8, RFC 4180 (pure parse/write).

Header must be exactly: name,location,height,building_count,floor_area,status,source_pdf.
Every raw cell is preserved verbatim for round-trip; invalid rows are kept with plain-language
findings and are never silently dropped.
height: '<positive finite number> m|metres|meters' passes through unchanged (no multiplier);
'<positive whole number> storeys|stories' (singular storey/story also accepted) converts by
the schematic rule 4.0 + 3.0 * (n - 1) + 1.5 m roof (10 storeys = 32.5 m). Blank or
'unknown' is unknown (null); there is no fallback height. Unitless numbers are rejected.
Whole numbers longer than 15 significant digits are rejected before int conversion, and the
computed height must be finite (no change to the interpreter's integer-digit limit).
building_count: positive whole number. floor_area: nonnegative finite square metres (plain
number, optional 'm2' suffix) or blank/unknown; never used to infer footprints.
name, location and status are required and copied verbatim (location is an exact join key).
source_pdf: http(s) URL with a non-empty hostname and, if present, a numeric port 1-65535; or
a 'test-only://' URL that later stages must treat as test-only. URLs are never fetched.
Geometry and provenance binding are validated in massing.csvvalidate.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar
from urllib.parse import urlsplit

COLUMNS: tuple[str, ...] = ("name", "location", "height", "building_count", "floor_area",
                            "status", "source_pdf")
TEST_ONLY_SCHEME = "test-only"
FIRST_STOREY_M = 4.0
TYPICAL_STOREY_M = 3.0
ROOF_ALLOWANCE_M = 1.5
STOREY_FORMULA = "4.0 + 3.0 * (n - 1) + 1.5 m roof"
MAX_INTEGER_DIGITS = 15
MAX_PORT = 65535
_UNKNOWN = frozenset({"", "unknown"})
_METRES = re.compile(r"(\d+(?:\.\d+)?)\s*(m|metres|meters)", re.IGNORECASE)
_STOREYS = re.compile(r"(\d+)\s*(storeys|stories|storey|story)", re.IGNORECASE)
_NUMBER_ONLY = re.compile(r"[+-]?\d+(?:\.\d+)?")
_COUNT = re.compile(r"\d+")
_AREA = re.compile(r"(\d+(?:\.\d+)?)\s*(?:m2)?", re.IGNORECASE)
_BOM = "\ufeff"
Severity = Literal["error", "note"]
T = TypeVar("T")


class CsvValueError(ValueError):
    """Invalid cell value; ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _show(raw: str) -> str:
    return repr(raw) if len(raw) <= 60 else repr(raw[:57] + "...")


def _bounded_int(digits: str, code: str, what: str) -> int:
    significant = digits.lstrip("0") or "0"
    if len(significant) > MAX_INTEGER_DIGITS:
        raise CsvValueError(code, f"{what} has more than {MAX_INTEGER_DIGITS} digits")
    return int(significant)


@dataclass(frozen=True, slots=True)
class CsvFinding:
    code: str
    message: str
    row_number: int | None = None
    line: int | None = None
    column: str | None = None
    raw: str | None = None
    severity: Severity = "error"


@dataclass(frozen=True, slots=True)
class HeightValue:
    raw: str
    known: bool
    height_m: float | None
    unit: Literal["m", "storeys"] | None
    storeys: int | None
    method: str


def storeys_to_metres(storeys: int) -> float:
    if isinstance(storeys, bool) or not isinstance(storeys, int) or storeys <= 0:
        raise CsvValueError("invalid_height", "storeys must be a positive whole number")
    if storeys >= 10 ** MAX_INTEGER_DIGITS:
        raise CsvValueError("invalid_height", f"storeys has more than {MAX_INTEGER_DIGITS} digits")
    value = FIRST_STOREY_M + TYPICAL_STOREY_M * float(storeys - 1) + ROOF_ALLOWANCE_M
    if not math.isfinite(value):
        raise CsvValueError("invalid_height", "storey count gives a non-finite height")
    return value


def parse_height(raw: str) -> HeightValue:
    text = raw.strip()
    if text.lower() in _UNKNOWN:
        return HeightValue(raw, False, None, None, None,
                           "unknown: blank or 'unknown' supplied; no fallback height")
    metres = _METRES.fullmatch(text)
    if metres:
        value = float(metres.group(1))
        if not math.isfinite(value) or value <= 0:
            raise CsvValueError("invalid_height",
                                f"height {_show(raw)} must be a positive finite number of metres")
        return HeightValue(raw, True, value, "m", None, "explicit metres, passed through unchanged")
    storeys = _STOREYS.fullmatch(text)
    if storeys:
        count = _bounded_int(storeys.group(1), "invalid_height", "storey count")
        if count <= 0:
            raise CsvValueError("invalid_height", f"height {_show(raw)} must have at least 1 storey")
        return HeightValue(raw, True, storeys_to_metres(count), "storeys", count,
                           f"schematic storey rule {STOREY_FORMULA} with n = {count}")
    if _NUMBER_ONLY.fullmatch(text):
        raise CsvValueError("unitless_height",
                            f"height {_show(raw)} has no unit; write e.g. '24 m' or '10 storeys'")
    raise CsvValueError("malformed_height",
                        f"height {_show(raw)} must be '<number> m', '<whole number> storeys', "
                        "blank or 'unknown'")


def parse_building_count(raw: str) -> int:
    text = raw.strip()
    if not _COUNT.fullmatch(text):
        raise CsvValueError("invalid_building_count",
                            f"building_count {_show(raw)} must be a positive whole number")
    count = _bounded_int(text, "invalid_building_count", "building_count")
    if count <= 0:
        raise CsvValueError("invalid_building_count",
                            f"building_count {_show(raw)} must be a positive whole number")
    return count


def parse_floor_area(raw: str) -> float | None:
    text = raw.strip()
    if text.lower() in _UNKNOWN:
        return None
    match = _AREA.fullmatch(text)
    value = float(match.group(1)) if match else math.nan
    if not math.isfinite(value) or value < 0:
        raise CsvValueError("invalid_floor_area", f"floor_area {_show(raw)} must be square "
                            "metres (e.g. 1200), blank or unknown")
    return value


def parse_source_url(raw: str) -> tuple[str, bool]:
    """Return (url, test_only). The URL is kept exactly; surrounding spaces are rejected.

    http/https URLs need a non-empty hostname and, when a port is written, a numeric port in
    1..65535. Any urllib parsing error is reported as invalid_source_pdf. Never fetched.
    """
    if not raw or raw != raw.strip() or any(c.isspace() for c in raw):
        raise CsvValueError("invalid_source_pdf",
                            f"source_pdf {_show(raw)} must be one URL without spaces")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise CsvValueError("invalid_source_pdf",
                            f"source_pdf {_show(raw)} is not a valid URL ({exc})") from exc
    if parts.scheme == TEST_ONLY_SCHEME and (parts.netloc or parts.path.strip("/")):
        return raw, True
    if parts.scheme in ("http", "https") and parts.netloc:
        try:
            hostname = parts.hostname
            port = parts.port
        except ValueError as exc:
            raise CsvValueError("invalid_source_pdf",
                                f"source_pdf {_show(raw)} has an invalid host or port ({exc})"
                                ) from exc
        if not hostname:
            raise CsvValueError("invalid_source_pdf",
                                f"source_pdf {_show(raw)} must name a host")
        if port is not None and not 1 <= port <= MAX_PORT:
            raise CsvValueError("invalid_source_pdf",
                                f"source_pdf {_show(raw)} port must be 1-{MAX_PORT}")
        return raw, False
    raise CsvValueError("invalid_source_pdf",
                        f"source_pdf {_show(raw)} must be an http(s) URL or a test-only:// URL")


@dataclass(frozen=True, slots=True)
class CsvRow:
    row_number: int
    line: int
    raw: tuple[str, ...]
    findings: tuple[CsvFinding, ...]
    name: str | None = None
    location: str | None = None
    height: HeightValue | None = None
    building_count: int | None = None
    floor_area_m2: float | None = None
    status: str | None = None
    source_pdf: str | None = None
    source_pdf_test_only: bool | None = None

    @property
    def valid(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    @property
    def height_known(self) -> bool:
        return self.height is not None and self.height.known


def _attempt(parse: Callable[[str], T], column: str, value: str, row: int, line: int,
             findings: list[CsvFinding]) -> T | None:
    try:
        return parse(value)
    except CsvValueError as exc:
        findings.append(CsvFinding(exc.code, str(exc), row, line, column, value))
        return None


def parse_row(cells: Sequence[str], row_number: int, line: int) -> CsvRow:
    raw = tuple(cells)
    if not raw:
        return CsvRow(row_number, line, raw, (CsvFinding(
            "blank_row", "row is blank; remove it or fill all seven columns", row_number, line),))
    if len(raw) != len(COLUMNS):
        return CsvRow(row_number, line, raw, (CsvFinding(
            "wrong_cell_count", f"row has {len(raw)} cells; exactly {len(COLUMNS)} are required",
            row_number, line),))
    values = dict(zip(COLUMNS, raw, strict=True))
    findings: list[CsvFinding] = []
    for column in ("name", "location", "status"):
        if not values[column].strip():
            findings.append(CsvFinding("missing_value", f"{column} is required", row_number,
                                       line, column, values[column]))
    height = _attempt(parse_height, "height", values["height"], row_number, line, findings)
    count = _attempt(parse_building_count, "building_count", values["building_count"],
                     row_number, line, findings)
    area = _attempt(parse_floor_area, "floor_area", values["floor_area"], row_number, line,
                    findings)
    source = _attempt(parse_source_url, "source_pdf", values["source_pdf"], row_number, line,
                      findings)
    return CsvRow(row_number, line, raw, tuple(findings), values["name"], values["location"],
                  height, count, area, values["status"],
                  source[0] if source else None, source[1] if source else None)


@dataclass(frozen=True, slots=True)
class CsvParseResult:
    header: tuple[str, ...] | None
    rows: tuple[CsvRow, ...]
    file_findings: tuple[CsvFinding, ...]

    @property
    def findings(self) -> tuple[CsvFinding, ...]:
        return (*self.file_findings, *(f for row in self.rows for f in row.findings))

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    @property
    def file_ok(self) -> bool:
        return not any(f.severity == "error" for f in self.file_findings)

    @property
    def valid_rows(self) -> tuple[CsvRow, ...]:
        return tuple(r for r in self.rows if r.valid)

    @property
    def rejected_rows(self) -> tuple[CsvRow, ...]:
        return tuple(r for r in self.rows if not r.valid)

    @property
    def unknown_height_rows(self) -> tuple[CsvRow, ...]:
        return tuple(r for r in self.rows if r.valid and not r.height_known)


def parse_csv_text(text: str) -> CsvParseResult:
    file_findings: list[CsvFinding] = []
    if text.startswith(_BOM):
        text = text[1:]
        file_findings.append(CsvFinding("bom_removed", "UTF-8 byte-order mark ignored",
                                        severity="note"))
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    records: list[tuple[int, list[str]]] = []
    try:
        for cells in reader:
            records.append((reader.line_num, cells))
    except csv.Error as exc:
        file_findings.append(CsvFinding("malformed_csv", f"CSV quoting is invalid near line "
                                        f"{reader.line_num}: {exc}", line=reader.line_num))
    if not records:
        file_findings.append(CsvFinding("missing_header", "file is empty; the header row is "
                                        f"required: {','.join(COLUMNS)}"))
        return CsvParseResult(None, (), tuple(file_findings))
    header = tuple(records[0][1])
    header_ok = header == COLUMNS
    if not header_ok:
        file_findings.append(CsvFinding("header_mismatch", f"header must be exactly "
                                        f"{','.join(COLUMNS)} in this order", 1, records[0][0]))
    if len(records) == 1:
        file_findings.append(CsvFinding("empty_data", "CSV has a header but no data rows"))
    rows: list[CsvRow] = []
    for number, (line, cells) in enumerate(records[1:], start=2):
        if header_ok:
            rows.append(parse_row(cells, number, line))
        else:
            rows.append(CsvRow(number, line, tuple(cells), (CsvFinding(
                "not_parsed", "row not parsed because the header is invalid", number, line),)))
    return CsvParseResult(header, tuple(rows), tuple(file_findings))


def parse_csv_bytes(data: bytes) -> CsvParseResult:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return CsvParseResult(None, (), (CsvFinding(
            "not_utf8", f"file is not valid UTF-8 (byte offset {exc.start})"),))
    return parse_csv_text(text)


def read_csv(path: Path) -> CsvParseResult:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return CsvParseResult(None, (), (CsvFinding("unreadable_file",
                                                    f"cannot read {path.name}: {exc.strerror}"),))
    return parse_csv_bytes(data)


@dataclass(frozen=True, slots=True)
class RoundtripCsv:
    text: str
    written_rows: tuple[int, ...]
    omitted_rows: tuple[int, ...]


def roundtrip_csv(result: CsvParseResult) -> RoundtripCsv:
    """Canonical header plus every seven-cell row's raw cells, CRLF, minimal quoting."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL, strict=True)
    writer.writerow(COLUMNS)
    written: list[int] = []
    omitted: list[int] = []
    for row in result.rows:
        if len(row.raw) == len(COLUMNS):
            writer.writerow(row.raw)
            written.append(row.row_number)
        else:
            omitted.append(row.row_number)
    return RoundtripCsv(buffer.getvalue(), tuple(written), tuple(omitted))


def roundtrip_preserved(result: CsvParseResult, text: str) -> bool:
    """True only when ``text`` is itself a valid CSV (no file-level errors) that re-parses to
    the canonical header and exactly the original seven-cell raw rows, in order.

    An ``empty_data`` re-parse error is tolerated only when the original had no seven-cell
    rows, since the faithful round-trip of such input is a header-only file.
    """
    again = parse_csv_text(text)
    original = [r.raw for r in result.rows if len(r.raw) == len(COLUMNS)]
    errors = {f.code for f in again.file_findings if f.severity == "error"}
    if not original:
        errors.discard("empty_data")
    return (not errors and again.header == COLUMNS
            and [r.raw for r in again.rows] == original)
