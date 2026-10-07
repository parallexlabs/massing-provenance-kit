"""Bind parsed CSV rows to explicit footprints and a provenance manifest (SPEC v2.0, pure).

No file access, clock or network. Any CSV file-level error, a footprint collection that is not
a FeatureCollection, a missing/wrong/malformed CRS (only EPSG:4326 name objects whose name is
a string are accepted) or a manifest that is not a JSON object is fatal: no row is modelled
from a partial input. When a fatal footprint-file error stops feature parsing, the raw number
of supplied features is still reported, manifest entries are still checked, and no
unbound_provenance findings are raised merely because features were not evaluated.
JSON documents must be strict UTF-8 JSON without duplicate keys and must re-serialise as
finite UTF-8 JSON (no NaN/Infinity, no exponent overflow, no lone surrogates anywhere).
Footprints: string Feature.id (filename-safe) equal to properties.id, exact name/location
strings, optional physical_building_id (defaults to id). A feature with an invalid id still
binds by its name/location so its row is rejected rather than silently passing without it;
the invalid id itself is never used. Feature ids are compared case-insensitively (casefold)
for duplicates, so ids that would collide as output file names are all rejected; physical ids
are compared exactly. Raw rings must have at least four positions, be explicitly closed and
contain finite [lon, lat] pairs; this is checked before Shapely sees them.
Polygons/MultiPolygons must be valid with positive area; nothing is repaired or simplified.
The original WGS84 geometry is kept; a projected EPSG:26910 copy is provided for meshing only.
Duplicate feature ids, duplicate physical ids and separate footprints overlapping with
positive area are rejected (every record involved).
Provenance manifest: object keyed by feature id; each entry needs location_point, source_url
(must equal the row source_pdf exactly), positive one-based page, unit consistent with the
row height, method, reviewer (an explicit statement such as 'unreviewed', never blank or
'unknown'), uncertainty, footprint_source_url, footprint_method, schematic true, detailed false
and boolean test_only (required true whenever any URL is test-only://). The location_point must
lie within or on its polygon (off_footprint otherwise). Other keys are preserved as extras.
Binding: rows match features by exact (name, location); building_count must equal the number
of distinct verified physical buildings bound to the row. Every member receives the row height.
Unknown heights produce a building with null height, no model and a missing_height error.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

import shapely
from shapely.geometry import MultiPolygon, Point, Polygon

from massing.csvio import COLUMNS, CsvParseResult, CsvRow, CsvValueError, parse_source_url
from massing.simplify import GeometryError, check_wgs84, to_projected

CRS_NAMES = frozenset({"EPSG:4326", "urn:ogc:def:crs:EPSG::4326"})
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
UNIT_ALIASES: Mapping[str, str] = {"m": "m", "metres": "m", "meters": "m", "storeys": "storeys",
                                   "stories": "storeys", "storey": "storeys", "story": "storeys"}
UNKNOWN_UNIT = "unknown"
REQUIRED_TEXT = ("source_url", "unit", "method", "reviewer", "uncertainty",
                 "footprint_source_url", "footprint_method")
MANIFEST_KEYS = frozenset({*REQUIRED_TEXT, "location_point", "page", "schematic", "detailed",
                           "test_only"})
UNREVIEWED_PLACEHOLDERS = frozenset({"", "unknown", "none", "n/a", "na"})
RESERVED_PROPERTIES = frozenset({
    "group_building_count", "group_floor_area_m2", "height_m", "base_height_m",
    "extrusion_height_m", "height_raw", "height_unit", "height_method", "evidence_source_url",
    "evidence_page", "evidence_unit", "evidence_method", "evidence_reviewer",
    "evidence_uncertainty", "evidence_test_only", "row_number", "schematic", "detailed",
    "provenance_file", "model_file", *(f"csv_{c}" for c in COLUMNS)})
Severity = Literal["error", "note"]
RowStatus = Literal["accepted", "unknown_height", "rejected", "not_evaluated"]


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    code: str
    message: str
    row_number: int | None = None
    feature_id: str | None = None
    severity: Severity = "error"


@dataclass(frozen=True, slots=True)
class Footprint:
    feature_id: str
    physical_id: str
    name: str
    location: str
    geometry: Mapping[str, Any]
    properties: Mapping[str, Any]
    geom_wgs84: Any
    geom_utm: Any
    area_m2: float
    geometry_sha256: str


@dataclass(frozen=True, slots=True)
class ProvenanceEntry:
    feature_id: str
    location_point: tuple[float, float]
    source_url: str
    page: int
    unit: str
    method: str
    reviewer: str
    uncertainty: str
    footprint_source_url: str
    footprint_method: str
    test_only: bool
    url_test_only: bool
    extras: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Building:
    footprint: Footprint
    provenance: ProvenanceEntry
    row: CsvRow

    @property
    def id(self) -> str:
        return self.footprint.feature_id

    @property
    def height_m(self) -> float | None:
        return self.row.height.height_m if self.row.height is not None else None

    @property
    def has_model(self) -> bool:
        return self.height_m is not None

    def provenance_record(self) -> dict[str, object]:
        fp, pv, row, h = self.footprint, self.provenance, self.row, self.row.height
        count = row.building_count or 0
        return {
            "id": fp.feature_id, "physical_building_id": fp.physical_id, "name": fp.name,
            "location": fp.location, "row_number": row.row_number,
            "source_url": pv.source_url, "page": pv.page, "input_unit": pv.unit,
            "raw_height": h.raw if h else None, "resolved_height_m": self.height_m,
            "height_unit": h.unit if h else None, "height_method": h.method if h else None,
            "source_method": pv.method, "reviewer": pv.reviewer, "uncertainty": pv.uncertainty,
            "status": row.status, "source_pdf": row.source_pdf, "building_count": count,
            "group_floor_area_m2": row.floor_area_m2, "group_floor_area_raw": row.raw[4],
            "uniform_height_assumption": (f"all {count} buildings in this row receive the row "
                                          "height" if count > 1 else None),
            "footprint_source_url": pv.footprint_source_url,
            "footprint_method": pv.footprint_method,
            "geometry_sha256": fp.geometry_sha256, "footprint_area_m2": round(fp.area_m2, 3),
            "location_point": list(pv.location_point), "schematic": True, "detailed": False,
            "test_only": pv.test_only, "footprint_properties": dict(fp.properties),
            "extras": dict(pv.extras),
        }

    def geojson_properties(self, provenance_file: str, model_file: str | None) -> dict[str, object]:
        if (model_file is None) == self.has_model:
            raise ValueError(f"{self.id}: model_file must be set exactly when height is known")
        h, pv, row = self.row.height, self.provenance, self.row
        props: dict[str, object] = dict(self.footprint.properties)
        props.update({f"csv_{c}": v for c, v in zip(COLUMNS, row.raw, strict=True)})
        props.update({
            "group_building_count": row.building_count, "group_floor_area_m2": row.floor_area_m2,
            "height_m": self.height_m, "base_height_m": 0.0, "extrusion_height_m": self.height_m,
            "height_raw": h.raw if h else None, "height_unit": h.unit if h else None,
            "height_method": h.method if h else None, "evidence_source_url": pv.source_url,
            "evidence_page": pv.page, "evidence_unit": pv.unit, "evidence_method": pv.method,
            "evidence_reviewer": pv.reviewer, "evidence_uncertainty": pv.uncertainty,
            "evidence_test_only": pv.test_only, "row_number": row.row_number,
            "schematic": True, "detailed": False, "provenance_file": provenance_file,
            "model_file": model_file})
        return props


@dataclass(frozen=True, slots=True)
class RowOutcome:
    row_number: int
    status: RowStatus
    feature_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Reconciliation:
    csv_rows: int
    accepted_rows: int
    unknown_rows: int
    rejected_rows: int
    not_evaluated_rows: int
    footprint_features: int
    output_buildings: int
    model_buildings: int
    unknown_buildings: int
    unmodelled_features: int
    reconciled: bool


@dataclass(frozen=True, slots=True)
class ValidationResult:
    rows: tuple[RowOutcome, ...]
    buildings: tuple[Building, ...]
    findings: tuple[ValidationFinding, ...]
    reconciliation: Reconciliation
    fatal: bool

    @property
    def ok(self) -> bool:
        return (not self.fatal and self.reconciliation.reconciled
                and not any(f.severity == "error" for f in self.findings))

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "fatal": self.fatal,
                "reconciliation": asdict(self.reconciliation),
                "rows": [asdict(r) for r in self.rows],
                "buildings": [{"id": b.id, "row_number": b.row.row_number,
                               "height_m": b.height_m, "has_model": b.has_model}
                              for b in self.buildings],
                "findings": [asdict(f) for f in self.findings]}


@dataclass(slots=True)
class _Record:
    feature_id: str | None
    physical_id: str | None
    name: str | None
    location: str | None
    footprint: Footprint | None
    entry: ProvenanceEntry | None = None
    label: str = ""

    @property
    def display(self) -> str:
        return self.feature_id if self.feature_id is not None else self.label


class _DuplicateKeyError(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = Counter(k for k, _ in pairs)
    duplicated = sorted(k for k, n in keys.items() if n > 1)
    if duplicated:
        raise _DuplicateKeyError(f"duplicate JSON keys {duplicated[:10]}")
    return dict(pairs)


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON number {name}")


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "..."


def parse_json_document(data: bytes, what: str) -> tuple[object | None, tuple[ValidationFinding, ...]]:
    """Strict JSON: UTF-8, no duplicate keys (duplicate ids), no NaN/Infinity, and the whole
    document must re-serialise as finite UTF-8 JSON (rejects exponent overflow such as 1e999
    and lone surrogates in any key or value, including nested extras)."""
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates,
                              parse_constant=_reject_constant)
    except _DuplicateKeyError as exc:
        return None, (ValidationFinding("duplicate_id", f"{what}: {exc}"),)
    except (UnicodeError, ValueError, RecursionError) as exc:
        return None, (ValidationFinding(f"malformed_{what}", f"{what} is not valid JSON: {exc}"),)
    try:
        json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError) as exc:
        return None, (ValidationFinding(
            f"malformed_{what}", f"{what} contains a non-finite number or invalid Unicode "
            f"text and cannot be kept as UTF-8 JSON: {exc}"),)
    return document, ()


def _position(raw: object) -> tuple[float, float]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise GeometryError("invalid_polygon", "each position must be [longitude, latitude]")
    values: list[float] = []
    for v in raw:
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise GeometryError("invalid_polygon", "coordinates must be numbers")
        try:
            values.append(float(v))
        except OverflowError as exc:
            raise GeometryError("invalid_polygon", "coordinate is too large") from exc
    lon, lat = values
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180
            and -90 <= lat <= 90):
        raise GeometryError("invalid_polygon", "coordinates must be finite WGS84 lon/lat")
    return lon, lat


def _ring(raw: object, what: str) -> list[tuple[float, float]]:
    if not isinstance(raw, list) or len(raw) < 4:
        raise GeometryError("invalid_polygon", f"{what} needs at least 4 positions")
    points = [_position(p) for p in raw]
    if points[0] != points[-1]:
        raise GeometryError("invalid_polygon", f"{what} is not closed (first and last differ)")
    return points


def _polygon(raw: object, what: str) -> Any:
    if not isinstance(raw, list) or not raw:
        raise GeometryError("invalid_polygon", f"{what} needs at least one ring")
    rings = [_ring(r, f"{what} ring {i}") for i, r in enumerate(raw)]
    return Polygon(rings[0], rings[1:])


def raw_geometry(geometry: object) -> Any:
    """Validate a raw GeoJSON Polygon/MultiPolygon without repairing or auto-closing it."""
    if not isinstance(geometry, dict):
        raise GeometryError("invalid_polygon", "footprint geometry is missing")
    kind, coords = geometry.get("type"), geometry.get("coordinates")
    if kind == "Polygon":
        geom = _polygon(coords, "polygon")
    elif kind == "MultiPolygon":
        if not isinstance(coords, list) or not coords:
            raise GeometryError("invalid_polygon", "multipolygon needs at least one polygon")
        geom = MultiPolygon([_polygon(p, f"part {i}") for i, p in enumerate(coords)])
    else:
        raise GeometryError("invalid_polygon", f"geometry type {kind!r} is not Polygon/MultiPolygon")
    if geom.is_empty or not shapely.is_valid(geom):
        raise GeometryError("invalid_polygon", f"polygon is invalid: {shapely.is_valid_reason(geom)}")
    if float(geom.area) <= 0:
        raise GeometryError("invalid_polygon", "polygon has zero area")
    return geom


def _is_epsg4326(crs: object) -> bool:
    """True only for {"type": "name", "properties": {"name": <accepted string>}}."""
    if not isinstance(crs, dict) or crs.get("type") != "name":
        return False
    props = crs.get("properties")
    if not isinstance(props, dict):
        return False
    name = props.get("name")
    return isinstance(name, str) and name in CRS_NAMES


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _feature(raw: object, index: int, findings: list[ValidationFinding]) -> _Record:
    label = f"features[{index}]"
    if not isinstance(raw, dict) or raw.get("type") != "Feature":
        findings.append(ValidationFinding("malformed_footprints", f"{label} is not a Feature"))
        return _Record(None, None, None, None, None, label=label)
    fid, props = raw.get("id"), raw.get("properties")
    if not isinstance(fid, str) or not SAFE_ID.fullmatch(fid) or not isinstance(props, dict):
        findings.append(ValidationFinding("invalid_id", f"{label} needs a filename-safe "
                                          "string id and a properties object; any row bound to "
                                          "its name and location is rejected"))
        if not isinstance(props, dict):
            return _Record(None, None, None, None, None, label=label)
        return _Record(None, None, _text_or_none(props.get("name")),
                       _text_or_none(props.get("location")), None, label=label)
    name, location = props.get("name"), props.get("location")
    physical = props.get("physical_building_id", fid)
    problems: list[tuple[str, str]] = []
    if props.get("id") != fid:
        problems.append(("invalid_id", "properties.id must equal Feature.id"))
    if not (isinstance(name, str) and name.strip() and isinstance(location, str)
            and location.strip()):
        problems.append(("missing_binding", "properties name and location are required"))
    if not isinstance(physical, str) or not SAFE_ID.fullmatch(physical):
        problems.append(("invalid_id", "physical_building_id must be a filename-safe string"))
    reserved = sorted(RESERVED_PROPERTIES & set(props))
    if reserved:
        problems.append(("reserved_property", f"properties use reserved output keys {reserved}"))
    footprint: Footprint | None = None
    try:
        wgs = raw_geometry(raw.get("geometry"))
        check_wgs84(wgs)
        utm = to_projected(wgs)
        if not shapely.is_valid(utm) or float(utm.area) <= 0:
            raise GeometryError("invalid_polygon", "polygon becomes invalid when projected")
    except GeometryError as exc:
        problems.append(("invalid_polygon", str(exc)))
    if not problems:
        text = json.dumps(raw["geometry"], sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
        footprint = Footprint(fid, str(physical), str(name), str(location), raw["geometry"],
                              props, wgs, utm, float(utm.area),
                              hashlib.sha256(text.encode("utf-8")).hexdigest())
    findings.extend(ValidationFinding(code, f"footprint {fid}: {msg}", feature_id=fid)
                    for code, msg in problems)
    return _Record(fid, physical if isinstance(physical, str) else None,
                   _text_or_none(name), _text_or_none(location), footprint, label=label)


def _footprints(raw: object, findings: list[ValidationFinding]) -> tuple[list[_Record], bool, int]:
    """Return (records, fatal, raw supplied feature count)."""
    if not isinstance(raw, dict) or raw.get("type") != "FeatureCollection":
        findings.append(ValidationFinding("malformed_footprints",
                                          "footprints must be a GeoJSON FeatureCollection"))
        return [], True, 0
    features = raw.get("features")
    feature_count = len(features) if isinstance(features, list) else 0
    fatal = False
    crs = raw.get("crs")
    if crs is None:
        findings.append(ValidationFinding("missing_crs", "footprints must declare crs EPSG:4326"))
        fatal = True
    elif not _is_epsg4326(crs):
        findings.append(ValidationFinding("wrong_crs", f"footprint crs {_short(crs)} is not an "
                                          "EPSG:4326 name object"))
        fatal = True
    if not isinstance(features, list):
        findings.append(ValidationFinding("malformed_footprints", "features must be a list"))
        fatal = True
    if fatal or not isinstance(features, list):
        return [], True, feature_count
    records = [_feature(f, i, findings) for i, f in enumerate(features)]
    id_counts = Counter(r.feature_id.casefold() for r in records if r.feature_id is not None)
    for r in records:
        if r.feature_id is not None and id_counts[r.feature_id.casefold()] > 1:
            findings.append(ValidationFinding(
                "duplicate_id", f"id {r.feature_id} collides with "
                f"{id_counts[r.feature_id.casefold()] - 1} other feature id(s) when compared "
                "case-insensitively; output file names must be unique", feature_id=r.feature_id))
            r.footprint = None
    physical_counts = Counter(r.physical_id for r in records if r.physical_id is not None)
    for r in records:
        if r.physical_id is not None and physical_counts[r.physical_id] > 1:
            findings.append(ValidationFinding(
                "duplicate_id", f"physical_building_id {r.physical_id} is used by "
                f"{physical_counts[r.physical_id]} features", feature_id=r.feature_id))
            r.footprint = None
    valid = [r for r in records if r.footprint is not None]
    tree = shapely.STRtree([r.footprint.geom_wgs84 for r in valid if r.footprint is not None])
    overlapping: set[int] = set()
    for i, r in enumerate(valid):
        assert r.footprint is not None
        for j in sorted(int(k) for k in tree.query(r.footprint.geom_wgs84, predicate="intersects")):
            other = valid[j].footprint
            if j <= i or other is None:
                continue
            if float(r.footprint.geom_wgs84.intersection(other.geom_wgs84).area) > 0:
                area = float(r.footprint.geom_utm.intersection(other.geom_utm).area)
                for a, b in ((r, valid[j]), (valid[j], r)):
                    findings.append(ValidationFinding(
                        "overlapping_footprints", f"footprint {a.feature_id} overlaps "
                        f"{b.feature_id} by {area:.3f} m2; supply one reviewed footprint",
                        feature_id=a.feature_id))
                overlapping.update((i, j))
    for i in overlapping:
        valid[i].footprint = None
    return records, False, feature_count


def _entry(fid: str, raw: object, findings: list[ValidationFinding]) -> ProvenanceEntry | None:
    def bad(code: str, message: str) -> None:
        findings.append(ValidationFinding(code, f"provenance {fid}: {message}", feature_id=fid))

    if not isinstance(raw, dict):
        bad("invalid_provenance", "entry must be an object")
        return None
    start = len(findings)
    for key in REQUIRED_TEXT:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            bad("invalid_provenance", f"{key} must be a non-empty string")
    reviewer = raw.get("reviewer")
    if isinstance(reviewer, str) and reviewer.strip().lower() in UNREVIEWED_PLACEHOLDERS:
        bad("invalid_provenance", "reviewer must be explicit, e.g. 'unreviewed'")
    page = raw.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        bad("invalid_provenance", "page must be a positive one-based integer")
    try:
        point = _position(raw.get("location_point"))
    except GeometryError:
        bad("invalid_provenance", "location_point must be finite [longitude, latitude]")
        point = (math.nan, math.nan)
    if raw.get("schematic") is not True or raw.get("detailed") is not False:
        bad("invalid_provenance", "schematic must be true and detailed must be false")
    test_only = raw.get("test_only")
    if not isinstance(test_only, bool):
        bad("invalid_provenance", "test_only must be true or false")
    unit = raw.get("unit")
    if isinstance(unit, str) and unit.strip().lower() not in {*UNIT_ALIASES, UNKNOWN_UNIT}:
        bad("malformed_unit", f"unit {unit!r} must be m/metres/meters, storeys/stories or unknown")
    url_flags: list[bool] = []
    for key in ("source_url", "footprint_source_url"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            try:
                url_flags.append(parse_source_url(value)[1])
            except CsvValueError as exc:
                bad("invalid_provenance", f"{key}: {exc}")
    if any(url_flags) and test_only is not True:
        bad("test_only_required", "test-only:// URLs require test_only true")
    if len(findings) > start:
        return None
    assert isinstance(page, int) and isinstance(test_only, bool)
    return ProvenanceEntry(fid, point, str(raw["source_url"]), page, str(unit), str(raw["method"]),
                           str(reviewer), str(raw["uncertainty"]), str(raw["footprint_source_url"]),
                           str(raw["footprint_method"]), test_only, any(url_flags),
                           {k: v for k, v in sorted(raw.items()) if k not in MANIFEST_KEYS})


def _row_problems(row: CsvRow, entry: ProvenanceEntry) -> list[str]:
    problems: list[str] = []
    if entry.source_url != row.source_pdf:
        problems.append(f"source_url {entry.source_url!r} differs from source_pdf")
    unit = entry.unit.strip().lower()
    if row.height is not None and row.height.known and UNIT_ALIASES.get(unit) != row.height.unit:
        problems.append(f"unit {entry.unit!r} is inconsistent with height {row.height.raw!r}")
    if row.source_pdf_test_only and not entry.test_only:
        problems.append("test-only source_pdf requires test_only true")
    return problems


def _finding_key(f: ValidationFinding) -> tuple[int, str, str, str]:
    return (f.row_number or 0, f.feature_id or "", f.code, f.message)


def validate(csv_result: CsvParseResult, footprints: object, manifest: object) -> ValidationResult:
    findings = [ValidationFinding(f.code, f.message, f.row_number, None, f.severity)
                for f in csv_result.findings]
    records, fp_fatal, feature_count = _footprints(footprints, findings)
    entries: dict[str, ProvenanceEntry] = {}
    pv_fatal = not isinstance(manifest, dict)
    if pv_fatal:
        findings.append(ValidationFinding("malformed_provenance",
                                          "provenance manifest must be a JSON object keyed by id"))
    else:
        assert isinstance(manifest, dict)
        for key in sorted(manifest):
            if not isinstance(key, str) or not SAFE_ID.fullmatch(key):
                findings.append(ValidationFinding("invalid_id", f"provenance key {key!r} is not safe"))
                continue
            entry = _entry(key, manifest[key], findings)
            if entry is not None:
                entries[key] = entry
        if not fp_fatal:
            known_ids = {r.feature_id for r in records if r.feature_id is not None}
            for key in sorted(set(manifest) - known_ids):
                findings.append(ValidationFinding("unbound_provenance", f"provenance {key} has no "
                                                  "footprint feature", feature_id=str(key)))
    fatal = not csv_result.file_ok or fp_fatal or pv_fatal
    rows: list[RowOutcome] = []
    buildings: list[Building] = []
    if fatal:
        rows = [RowOutcome(r.row_number, "not_evaluated", ()) for r in csv_result.rows]
    else:
        assert isinstance(manifest, dict)
        for rec in records:
            fp = rec.footprint
            if fp is None or rec.feature_id is None:
                continue
            entry = entries.get(rec.feature_id)
            if entry is None:
                code = "invalid_provenance" if rec.feature_id in manifest else "missing_provenance"
                findings.append(ValidationFinding(code, f"footprint {rec.feature_id} has no valid "
                                                  "provenance entry", feature_id=rec.feature_id))
                rec.footprint = None
            elif not fp.geom_wgs84.covers(Point(entry.location_point)):
                findings.append(ValidationFinding("off_footprint", f"location_point of "
                                                  f"{rec.feature_id} is outside its footprint",
                                                  feature_id=rec.feature_id))
                rec.footprint = None
            else:
                rec.entry = entry
        by_key: dict[tuple[str, str], list[_Record]] = defaultdict(list)
        for rec in records:
            if rec.name is not None and rec.location is not None:
                by_key[(rec.name, rec.location)].append(rec)
        row_keys = Counter((r.name, r.location) for r in csv_result.rows
                           if r.name is not None and r.location is not None)
        for row in csv_result.rows:
            key = (row.name or "", row.location or "")
            bound = by_key.get(key, [])
            ids = tuple(sorted(r.feature_id for r in bound if r.feature_id))
            problems: list[tuple[str, str]] = []
            if not row.valid:
                rows.append(RowOutcome(row.row_number, "rejected", ids))
                continue
            if row_keys[key] > 1:
                problems.append(("conflicting_binding", "another row has the same name and location"))
            elif not bound:
                problems.append(("missing_footprint", "no footprint feature has this exact name and "
                                 "location; footprints are never derived or guessed"))
            elif any(r.footprint is None or r.entry is None for r in bound):
                failed = sorted(r.display for r in bound if r.footprint is None or r.entry is None)
                problems.append(("invalid_footprint", f"bound footprints {failed} failed "
                                 "id, geometry or provenance checks"))
            else:
                physical = {r.physical_id for r in bound}
                if len(physical) != row.building_count:
                    problems.append(("building_count_mismatch", f"building_count "
                                     f"{row.building_count} but {len(physical)} distinct verified "
                                     "physical buildings are bound"))
                for rec in bound:
                    assert rec.entry is not None
                    problems.extend(("provenance_mismatch", f"{rec.feature_id}: {p}")
                                    for p in _row_problems(row, rec.entry))
            if problems:
                findings.extend(ValidationFinding(c, f"row {row.row_number}: {m}", row.row_number)
                                for c, m in problems)
                rows.append(RowOutcome(row.row_number, "rejected", ids))
                continue
            for rec in bound:
                assert rec.footprint is not None and rec.entry is not None
                buildings.append(Building(rec.footprint, rec.entry, row))
            if row.height_known:
                rows.append(RowOutcome(row.row_number, "accepted", ids))
            else:
                findings.append(ValidationFinding(
                    "missing_height", f"row {row.row_number}: height is blank or unknown, so no "
                    "model is produced; supply '<number> m' or '<whole number> storeys'",
                    row.row_number))
                rows.append(RowOutcome(row.row_number, "unknown_height", ids))
        for key, recs in sorted(by_key.items()):
            if row_keys[key] == 0:
                findings.extend(ValidationFinding("unbound_footprint", f"footprint {r.display} "
                                                  "matches no CSV row", feature_id=r.feature_id)
                                for r in recs)
    buildings.sort(key=lambda b: b.id)
    status = Counter(r.status for r in rows)
    models = sum(1 for b in buildings if b.has_model)
    expected = sum(r.building_count or 0 for r in csv_result.rows
                   if any(o.row_number == r.row_number and o.status in ("accepted", "unknown_height")
                          for o in rows))
    reconciled = (sum(status.values()) == len(csv_result.rows) and expected == len(buildings)
                  and len(buildings) <= feature_count)
    reconciliation = Reconciliation(
        len(csv_result.rows), status["accepted"], status["unknown_height"], status["rejected"],
        status["not_evaluated"], feature_count, len(buildings), models, len(buildings) - models,
        feature_count - len(buildings), reconciled)
    return ValidationResult(tuple(rows), tuple(buildings), tuple(sorted(findings, key=_finding_key)),
                            reconciliation, fatal)
