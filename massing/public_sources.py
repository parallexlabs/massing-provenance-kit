"""Bounded read-only access to the official City of Surrey FeatureServers (SPEC URLs only).

Selection: layer metadata (f=json) -> returnIdsOnly within a validated WGS84 bbox ->
ids sorted ascending -> first ``max_features`` selected (truncation is recorded, never
hidden) -> paged objectIds queries with allowlisted outFields only.
Integrity: the ids response must contain the ``objectIds`` key (an explicit null is the
documented ArcGIS form of "no records"; a missing key is a failure); returnCountOnly must
contain an integer, non-boolean, nonnegative ``count`` equal to the id count. Non-integer
or duplicate ids, missing/extra/duplicate records per page, exceededTransferLimit,
HTTP/JSON/service errors and allowlisted fields absent from service metadata all raise
SourceError. Nothing is fabricated on failure. Every request is recorded with UTC
timestamp, sorted parameters, byte count and SHA-256 of the raw body.
Surrey BUILDING_HEIGHT is kept as a source attribute only: it is NOT ladder evidence and
never a City-supplied override (overrides require a reviewed override CSV).
Applicant and contact attributes are never requested.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import httpx

from massing import MassingError, __version__
from massing.simplify import projected_area_of_use

FOOTPRINTS_URL = "https://services5.arcgis.com/YRpe0VKTJytZSSIB/arcgis/rest/services/Building%20Footprnts/FeatureServer/0"
APPLICATIONS_URL = "https://services5.arcgis.com/YRpe0VKTJytZSSIB/arcgis/rest/services/Development%20Applications/FeatureServer/0"
SURREY_LICENCE = "Open Government License \u2013 City of Surrey"
SURREY_LICENCE_URL = "https://opendata-surrey.hub.arcgis.com/pages/55089a19491a4fe59a41e059fd8af708"
USER_AGENT = f"massing-provenance-kit/{__version__} (read-only)"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_RESPONSE_BYTES = 25_000_000
MAX_FEATURES = 500
PAGE_SIZE = 100
MAX_BBOX_SPAN_DEG = 0.05
NOT_EVIDENCE_FIELDS = ("BUILDING_HEIGHT",)
BBox = tuple[float, float, float, float]
Clock = Callable[[], datetime]


class SourceError(MassingError):
    """Public-source failure; ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise SourceError("naive_timestamp", "clock must return timezone-aware datetimes")
    return moment.astimezone(UTC).isoformat()


def validate_bbox(raw: object) -> BBox:
    """(west, south, east, north) WGS84, finite, ordered, <= 0.05 deg span, in EPSG:26910 use."""
    if isinstance(raw, str | bytes) or not isinstance(raw, Sequence) or len(raw) != 4:
        raise SourceError("invalid_bbox", "bbox must be (west, south, east, north)")
    values: list[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int | float) \
                or not math.isfinite(value):
            raise SourceError("invalid_bbox", f"bbox value {value!r} is not a finite number")
        values.append(float(value))
    west, south, east, north = values
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise SourceError("invalid_bbox", "bbox must satisfy west < east and south < north")
    if east - west > MAX_BBOX_SPAN_DEG or north - south > MAX_BBOX_SPAN_DEG:
        raise SourceError("bbox_too_large", f"bbox span exceeds {MAX_BBOX_SPAN_DEG} degrees")
    aw, as_, ae, an = projected_area_of_use()
    if west < aw or east > ae or south < as_ or north > an:
        raise SourceError("invalid_bbox", "bbox is outside the EPSG:26910 area of use")
    return (west, south, east, north)


@dataclass(frozen=True, slots=True)
class RequestRecord:
    url: str
    params: tuple[tuple[str, str], ...]
    retrieved_at: str
    status: int
    sha256: str
    bytes: int
    record_count: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {"method": "GET", "url": self.url, "params": dict(self.params),
                "retrieved_at": self.retrieved_at, "status": self.status,
                "sha256": self.sha256, "bytes": self.bytes, "record_count": self.record_count}


def default_client() -> httpx.Client:
    return httpx.Client(timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT},
                        follow_redirects=False)


def http_get(client: httpx.Client, url: str, params: Mapping[str, str], *, now: Clock
             ) -> tuple[bytes, RequestRecord]:
    ordered = tuple(sorted(params.items()))
    try:
        response = client.get(url, params=ordered, timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as exc:
        raise SourceError("http_error", f"GET {url} failed: {exc}") from exc
    stamp = _iso(now())
    body = response.content
    if response.status_code != 200:
        raise SourceError("http_status", f"GET {url} returned HTTP {response.status_code}")
    if len(body) > MAX_RESPONSE_BYTES:
        raise SourceError("response_too_large", f"GET {url} exceeded {MAX_RESPONSE_BYTES} bytes")
    return body, RequestRecord(url, ordered, stamp, response.status_code, sha256_hex(body),
                               len(body))


def parse_json_object(body: bytes, url: str) -> dict[str, Any]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceError("invalid_json", f"{url}: {exc}") from exc
    if not isinstance(data, dict):
        raise SourceError("invalid_json", f"{url}: expected a JSON object")
    if data.get("error"):
        raise SourceError("service_error", f"{url}: {str(data['error'])[:300]}")
    return data


@dataclass(frozen=True, slots=True)
class FeatureServerSource:
    name: str
    dataset: str
    url: str
    fields: tuple[str, ...]
    licence: str = SURREY_LICENCE
    licence_url: str = SURREY_LICENCE_URL
    id_field: str = "OBJECTID"


FOOTPRINTS = FeatureServerSource("footprints", "Building Footprints", FOOTPRINTS_URL,
                                 ("OBJECTID", "FACILITY_TYPE", "BUILDING_HEIGHT", "STATUS"))
APPLICATIONS = FeatureServerSource("development_applications", "Development Applications",
                                   APPLICATIONS_URL,
                                   ("OBJECTID", "PROJECT_NO", "DESCRIPTION", "STATUS", "WEBLINK"))
OFFICIAL_SOURCES: tuple[FeatureServerSource, ...] = (FOOTPRINTS, APPLICATIONS)


@dataclass(frozen=True, slots=True)
class LayerSnapshot:
    source: FeatureServerSource
    bbox: BBox
    service_name: str | None
    max_record_count: int
    editing_info: Mapping[str, int]
    licence_info: str | None
    total_bbox_count: int
    selected_ids: tuple[int, ...]
    truncated: bool
    features: tuple[dict[str, Any], ...]
    requests: tuple[RequestRecord, ...]

    def features_sha256(self) -> str:
        text = json.dumps(list(self.features), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
        return sha256_hex(text.encode("utf-8"))

    def manifest(self) -> dict[str, object]:
        return {
            "name": self.source.name, "dataset": self.source.dataset,
            "endpoint": self.source.url, "licence": self.source.licence,
            "licence_url": self.source.licence_url, "service_licence_info": self.licence_info,
            "bbox": list(self.bbox), "service_name": self.service_name,
            "max_record_count": self.max_record_count, "editing_info": dict(self.editing_info),
            "total_bbox_count": self.total_bbox_count, "selected_count": len(self.selected_ids),
            "truncated": self.truncated,
            "selection": f"first {len(self.selected_ids)} of {self.total_bbox_count} OBJECTIDs "
                         "(ascending) intersecting the bbox; bounded subset, not an inventory",
            "fields": list(self.source.fields),
            "not_evidence_fields": list(NOT_EVIDENCE_FIELDS),
            "features_sha256": self.features_sha256(),
            "requests": [r.to_dict() for r in self.requests], "client": USER_AGENT,
        }

    def feature_collection(self) -> dict[str, object]:
        return {"type": "FeatureCollection", "features": list(self.features),
                "manifest": self.manifest()}


def _int_ids(raw: object, url: str) -> list[int]:
    """objectIds value: explicit null means no records; otherwise a list of unique ints."""
    if raw is None:
        return []
    if not isinstance(raw, list) or any(isinstance(i, bool) or not isinstance(i, int)
                                        for i in raw):
        raise SourceError("invalid_ids", f"{url}: objectIds must be a list of integers")
    if len(set(raw)) != len(raw):
        raise SourceError("duplicate_ids", f"{url}: duplicate objectIds returned")
    return sorted(raw)


def _strict_count(payload: Mapping[str, Any], url: str) -> int:
    if "count" not in payload:
        raise SourceError("missing_count", f"{url}: returnCountOnly response lacks count")
    count: object = payload["count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise SourceError("invalid_count", f"{url}: count {count!r} is not a nonnegative int")
    return count


def fetch_layer(source: FeatureServerSource, bbox: object, *, client: httpx.Client,
                max_features: int = MAX_FEATURES, page_size: int = PAGE_SIZE,
                now: Clock = utc_now) -> LayerSnapshot:
    if source not in OFFICIAL_SOURCES:
        raise SourceError("unofficial_source", f"{source.url} is not an approved SPEC endpoint")
    box = validate_bbox(bbox)
    for label, value, limit in (("max_features", max_features, MAX_FEATURES),
                                ("page_size", page_size, PAGE_SIZE)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
            raise SourceError("invalid_limit", f"{label} must be an int in 1..{limit}")
    records: list[RequestRecord] = []
    body, record = http_get(client, source.url, {"f": "json"}, now=now)
    records.append(record)
    meta = parse_json_object(body, source.url)
    max_rc = meta.get("maxRecordCount")
    if isinstance(max_rc, bool) or not isinstance(max_rc, int) or max_rc <= 0:
        raise SourceError("invalid_metadata", f"{source.url}: maxRecordCount missing or invalid")
    raw_fields = meta.get("fields")
    names = {f.get("name") for f in raw_fields if isinstance(f, dict)} \
        if isinstance(raw_fields, list) else set()
    missing = [f for f in source.fields if f not in names]
    if missing:
        raise SourceError("missing_fields", f"{source.url}: service lacks fields {missing}")
    if meta.get("objectIdField") not in (None, source.id_field):
        raise SourceError("id_field_mismatch", f"{source.url}: objectIdField differs")
    editing = meta.get("editingInfo")
    editing_info = {k: v for k, v in editing.items() if isinstance(v, int)
                    and not isinstance(v, bool)} if isinstance(editing, dict) else {}
    query_url = f"{source.url}/query"
    spatial = {"where": "1=1", "geometry": ",".join(f"{v:.7f}" for v in box),
               "geometryType": "esriGeometryEnvelope", "inSR": "4326",
               "spatialRel": "esriSpatialRelIntersects"}
    body, record = http_get(client, query_url, {**spatial, "returnIdsOnly": "true", "f": "json"},
                            now=now)
    ids_payload = parse_json_object(body, query_url)
    if "objectIds" not in ids_payload:
        raise SourceError("missing_ids",
                          f"{query_url}: returnIdsOnly response lacks objectIds (null = none)")
    ids = _int_ids(ids_payload["objectIds"], query_url)
    records.append(replace(record, record_count=len(ids)))
    body, record = http_get(client, query_url,
                            {**spatial, "returnCountOnly": "true", "f": "json"}, now=now)
    count = _strict_count(parse_json_object(body, query_url), query_url)
    records.append(replace(record, record_count=count))
    if count != len(ids):
        raise SourceError("count_mismatch", f"count {count} != {len(ids)} objectIds")
    selected = ids[:max_features]
    size = min(page_size, max_rc)
    features: list[dict[str, Any]] = []
    for start in range(0, len(selected), size):
        chunk = selected[start:start + size]
        params = {"objectIds": ",".join(str(i) for i in chunk),
                  "outFields": ",".join(source.fields), "outSR": "4326",
                  "returnGeometry": "true", "f": "geojson"}
        body, record = http_get(client, query_url, params, now=now)
        data = parse_json_object(body, query_url)
        props = data.get("properties")
        if data.get("exceededTransferLimit") is True or (
                isinstance(props, dict) and props.get("exceededTransferLimit") is True):
            raise SourceError("transfer_limit", f"{query_url}: page exceeded transfer limit")
        raw_features = data.get("features")
        if not isinstance(raw_features, list):
            raise SourceError("invalid_payload", f"{query_url}: features must be a list")
        got: list[int] = []
        for feature in raw_features:
            attrs = feature.get("properties") if isinstance(feature, dict) else None
            oid = attrs.get(source.id_field) if isinstance(attrs, dict) else None
            if isinstance(oid, bool) or not isinstance(oid, int) or not isinstance(attrs, dict):
                raise SourceError("invalid_record", f"{query_url}: feature without integer id")
            got.append(oid)
            clean = {k: attrs.get(k) for k in source.fields}
            clean.update({"source_dataset": source.dataset, "source_url": source.url,
                          "retrieved_at": record.retrieved_at, "licence": source.licence,
                          "licence_url": source.licence_url, "source_sha256": record.sha256})
            features.append({"type": "Feature", "id": oid,
                             "geometry": feature.get("geometry"), "properties": clean})
        if len(set(got)) != len(got):
            raise SourceError("duplicate_records", f"{query_url}: duplicate records in page")
        if set(got) != set(chunk):
            code = "missing_records" if set(got) < set(chunk) else "unexpected_records"
            raise SourceError(code, f"{query_url}: page ids differ from requested ids")
        records.append(replace(record, record_count=len(raw_features)))
    raw_name = meta.get("name")
    service_name: str | None = raw_name if isinstance(raw_name, str) else None
    raw_licence = meta.get("copyrightText")
    licence_info: str | None = raw_licence if isinstance(raw_licence, str) else None
    return LayerSnapshot(source, box, service_name, max_rc, editing_info, licence_info,
                         len(ids), tuple(selected), len(ids) > max_features,
                         tuple(sorted(features, key=lambda f: int(f["id"]))), tuple(records))


def _feature_id(feature: Mapping[str, Any]) -> object:
    props = feature.get("properties")
    oid = props.get("OBJECTID") if isinstance(props, Mapping) else None
    return str(oid) if isinstance(oid, int) and not isinstance(oid, bool) else oid


def footprint_items(features: Sequence[Mapping[str, Any]]) -> list[tuple[object, object]]:
    """(stable id = str(OBJECTID), GeoJSON geometry) for simplify.prepare_footprints."""
    return [(_feature_id(f), f.get("geometry")) for f in features]


_STOREY_WORD = re.compile(r"\bstor(?:ey|eys|y|ies)\b", re.IGNORECASE)
_RANGE = re.compile(r"\d+(?:\.\d+)?\s*(?:-|\u2013|\u2014|to)\s*\d+(?:\.\d+)?\s*-?\s*$",
                    re.IGNORECASE)
_DECIMAL = re.compile(r"\d+\.\d+\s*-?\s*$")
_NEGATIVE = re.compile(r"(?<![\w.])-\s*\d+\s*-?\s*$")
_INTEGER = re.compile(r"(?<![\d.])(\d+)\s*-?\s*$")
_WORD_COUNT = re.compile(
    r"\b(?:single|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"multi|multiple|several|double|triple)\s*-?\s*$", re.IGNORECASE)


def extract_storeys(text: object) -> tuple[int | None, str]:
    """Return (storeys, status). Exactly one plain positive-integer storey clause is accepted.

    Ranges, decimals, negatives, word-only counts and any description with more than one
    count clause (even repeating the same number) yield None with an explicit status.
    Large positive integers are retained as stated; QA flags counts above 60.
    """
    if not isinstance(text, str) or not text.strip():
        return None, "no_text"
    kinds: list[str] = []
    values: list[int] = []
    for match in _STOREY_WORD.finditer(text):
        before = text[max(0, match.start() - 30):match.start()]
        if _RANGE.search(before):
            return None, "range_storeys"
        if _DECIMAL.search(before):
            return None, "decimal_storeys"
        if _NEGATIVE.search(before):
            return None, "negative_storeys"
        integer = _INTEGER.search(before)
        if integer:
            kinds.append("numeric")
            values.append(int(integer.group(1)))
        elif _WORD_COUNT.search(before):
            kinds.append("word")
    if not kinds:
        return None, "no_explicit_storeys"
    if len(kinds) > 1:
        return None, f"multiple_storey_clauses:{len(kinds)}"
    if kinds[0] == "word":
        return None, "non_numeric_storeys"
    if values[0] <= 0:
        return None, "nonpositive_storeys"
    return values[0], "explicit_storeys"


@dataclass(frozen=True, slots=True)
class ApplicationRecord:
    site_id: object
    geometry: object
    project_ref: str | None
    description: str | None
    weblink: str | None
    status: str | None
    storeys: int | None
    storeys_status: str
    retrieved_at: str | None

    @property
    def reference(self) -> str | None:
        return self.weblink or self.project_ref


def _text(values: Mapping[str, Any], key: str) -> str | None:
    value = values.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) and value.strip() else None


def application_records(features: Sequence[Mapping[str, Any]]) -> tuple[ApplicationRecord, ...]:
    """Application sites with raw STATUS and conservatively extracted storeys."""
    out: list[ApplicationRecord] = []
    for feature in features:
        props = feature.get("properties")
        attrs: Mapping[str, Any] = props if isinstance(props, Mapping) else {}
        storeys, storeys_status = extract_storeys(attrs.get("DESCRIPTION"))
        out.append(ApplicationRecord(
            site_id=_feature_id(feature), geometry=feature.get("geometry"),
            project_ref=_text(attrs, "PROJECT_NO"), description=_text(attrs, "DESCRIPTION"),
            weblink=_text(attrs, "WEBLINK"), status=_text(attrs, "STATUS"),
            storeys=storeys, storeys_status=storeys_status,
            retrieved_at=_text(attrs, "retrieved_at")))
    return tuple(out)
