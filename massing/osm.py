"""Bounded Overpass access, native OSM closed-way parsing and conservative footprint join.

Query: ways tagged building with height or building:levels inside a validated bbox,
``out tags geom`` limited to MAX_ELEMENTS + 1; more than MAX_ELEMENTS is an explicit
truncation failure. Cache files hold query, query SHA-256, retrieval date, payload SHA-256
and raw payload text; any mismatch is a cache_integrity failure, never a silent refetch.
Only closed ways (>= 4 points, first == last) with valid non-zero area in EPSG:26910 are
used; relations, nodes, unclosed/invalid/duplicate ways are reported, never repaired.
Join (EPSG:26910): a way/footprint pair is accepted only if the intersection covers at
least 80% of BOTH areas and the pairing is one-to-one. Ways with several strong
candidates, footprints claimed by several ways, weak overlaps and unmatched ways are
findings with no evidence; identity is never guessed.
Synthetic fixtures (top-level test_only: true) keep their own fixture provenance and get
"test-only:" references. OSM data: (c) OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import shapely
from shapely.geometry import Polygon

from massing.heights import EvidenceCandidate, HeightSource
from massing.public_sources import (
    Clock,
    SourceError,
    http_get,
    parse_json_object,
    sha256_hex,
    utc_now,
    validate_bbox,
)
from massing.simplify import GeometryError, check_wgs84, to_projected

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSM_LICENCE = "\u00a9 OpenStreetMap contributors, ODbL"
OSM_TAGS = ("building", "height", "building:levels", "min_height")
OVERPASS_TIMEOUT_S = 25
MAX_ELEMENTS = 5000
STRONG_OVERLAP = 0.8
_CACHE_KEYS = frozenset({"query", "query_sha256", "retrieved_at", "payload_sha256",
                         "payload_text"})


@dataclass(frozen=True, slots=True)
class OsmProvenance:
    source_url: str
    query: str | None
    retrieved_at: str
    payload_sha256: str
    licence: str
    test_only: bool = False
    osm_base: str | None = None
    from_cache: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OsmBuilding:
    osm_id: str
    geom_utm: Any
    tags: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class OsmFinding:
    subject: str
    subject_id: str
    code: str
    detail: str
    candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OsmMatch:
    footprint_id: str
    osm_id: str
    footprint_overlap: float
    way_overlap: float


@dataclass(frozen=True, slots=True)
class OsmEvidence:
    footprint_id: str
    osm_id: str
    candidates: tuple[EvidenceCandidate, ...]
    min_height: str | None
    min_height_reference: str | None
    use_class: str | None
    licence: str
    retrieved_at: str
    test_only: bool


@dataclass(frozen=True, slots=True)
class OsmJoinResult:
    provenance: OsmProvenance
    element_count: int
    usable_way_count: int
    matches: tuple[OsmMatch, ...]
    evidence: Mapping[str, OsmEvidence]
    findings: tuple[OsmFinding, ...]
    unmatched_footprint_ids: tuple[str, ...]


def overpass_query(bbox: object, timeout_s: int = OVERPASS_TIMEOUT_S) -> str:
    west, south, east, north = validate_bbox(bbox)
    box = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    return (f'[out:json][timeout:{timeout_s}];(way["building"]["height"]({box});'
            f'way["building"]["building:levels"]({box}););out tags geom {MAX_ELEMENTS + 1};')


def load_osm_payload(body: bytes, *, retrieved_at: str | None, query: str | None = None,
                     source_url: str = OVERPASS_URL, from_cache: bool = False
                     ) -> tuple[dict[str, Any], OsmProvenance]:
    """Parse a raw Overpass/fixture body; real data needs an explicit retrieval date."""
    payload = parse_json_object(body, source_url)
    remark = payload.get("remark")
    if isinstance(remark, str) and ("error" in remark.lower() or "timed out" in remark.lower()):
        raise SourceError("overpass_error", f"Overpass remark: {remark[:300]}")
    if not isinstance(payload.get("elements"), list):
        raise SourceError("invalid_payload", "Overpass payload has no elements list")
    digest = sha256_hex(body)
    if payload.get("test_only") is True:
        fixture = payload.get("fixture_provenance")
        if not isinstance(fixture, dict):
            raise SourceError("missing_provenance", "test-only payload lacks fixture_provenance")
        url, stamp, licence = (fixture.get(k) for k in ("source_url", "retrieved_at", "licence"))
        if not (isinstance(url, str) and isinstance(stamp, str) and isinstance(licence, str)):
            raise SourceError("missing_provenance", "fixture_provenance needs url/date/licence")
        return payload, OsmProvenance(url, query, stamp, digest, licence, True, None, from_cache)
    if not isinstance(retrieved_at, str) or not retrieved_at:
        raise SourceError("missing_provenance", "real OSM payload requires retrieved_at")
    meta = payload.get("osm3s")
    base = meta.get("timestamp_osm_base") if isinstance(meta, dict) else None
    return payload, OsmProvenance(source_url, query, retrieved_at, digest, OSM_LICENCE, False,
                                  base if isinstance(base, str) else None, from_cache)


def _write_cache(path: Path, query: str, retrieved_at: str, body: bytes) -> None:
    record = {"query": query, "query_sha256": sha256_hex(query.encode("utf-8")),
              "retrieved_at": retrieved_at, "payload_sha256": sha256_hex(body),
              "payload_text": body.decode("utf-8")}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_cache(path: Path, query: str) -> tuple[dict[str, Any], OsmProvenance]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError("cache_unreadable", f"{path}: {exc}") from exc
    if not isinstance(record, dict) or set(record) != _CACHE_KEYS:
        raise SourceError("cache_integrity", f"{path}: unexpected cache structure")
    text, stamp = record["payload_text"], record["retrieved_at"]
    if record["query"] != query or record["query_sha256"] != sha256_hex(query.encode("utf-8")):
        raise SourceError("cache_integrity", f"{path}: cached query differs from request")
    if not isinstance(text, str) or not isinstance(stamp, str) or not stamp:
        raise SourceError("cache_integrity", f"{path}: payload or retrieval date missing")
    body = text.encode("utf-8")
    if sha256_hex(body) != record["payload_sha256"]:
        raise SourceError("cache_integrity", f"{path}: payload hash mismatch")
    payload, provenance = load_osm_payload(body, retrieved_at=stamp, query=query,
                                           from_cache=True)
    if provenance.test_only:
        raise SourceError("cache_integrity", f"{path}: cache holds a test-only payload")
    return payload, provenance


def fetch_overpass(bbox: object, *, client: httpx.Client, cache_dir: Path | None = None,
                   refresh: bool = False, now: Clock = utc_now
                   ) -> tuple[dict[str, Any], OsmProvenance]:
    """Bounded read-only Overpass GET; reuses an integrity-checked cache unless refresh."""
    query = overpass_query(bbox)
    path = None if cache_dir is None else \
        cache_dir / f"overpass-{sha256_hex(query.encode('utf-8'))[:32]}.json"
    if path is not None and path.exists() and not refresh:
        return read_cache(path, query)
    body, record = http_get(client, OVERPASS_URL, {"data": query}, now=now)
    payload, provenance = load_osm_payload(body, retrieved_at=record.retrieved_at, query=query)
    if provenance.test_only:
        raise SourceError("invalid_payload", "live Overpass response claims to be test-only")
    if path is not None:
        _write_cache(path, query, record.retrieved_at, body)
    return payload, provenance


def _points(raw: object) -> list[tuple[float, float]] | None:
    if not isinstance(raw, list):
        return None
    points: list[tuple[float, float]] = []
    for node in raw:
        if not isinstance(node, dict):
            return None
        lon, lat = node.get("lon"), node.get("lat")
        if isinstance(lon, bool) or isinstance(lat, bool) \
                or not isinstance(lon, int | float) or not isinstance(lat, int | float) \
                or not (math.isfinite(lon) and math.isfinite(lat)):
            return None
        points.append((float(lon), float(lat)))
    return points


def parse_ways(payload: Mapping[str, Any]) -> tuple[list[OsmBuilding], list[OsmFinding]]:
    """Closed native ways only; every rejected element becomes a finding."""
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise SourceError("invalid_payload", "elements must be a list")
    if len(elements) > MAX_ELEMENTS:
        raise SourceError("overpass_truncated",
                          f"more than {MAX_ELEMENTS} elements; use a smaller bbox")
    keys = [f"{e.get('type')}/{e.get('id')}" if isinstance(e, dict) else "?" for e in elements]
    counts = Counter(keys)
    ways: list[OsmBuilding] = []
    findings: list[OsmFinding] = []
    for key, element in zip(keys, elements, strict=True):
        if not isinstance(element, dict):
            findings.append(OsmFinding("osm_element", key, "malformed_element", "not an object"))
            continue
        oid = element.get("id")
        if element.get("type") != "way" or isinstance(oid, bool) or not isinstance(oid, int):
            findings.append(OsmFinding("osm_element", key, "unsupported_element",
                                       "only native OSM ways are used; relations/nodes ignored"))
            continue
        if counts[key] > 1:
            findings.append(OsmFinding("osm_way", key, "duplicate_element",
                                       f"{key} appears {counts[key]} times; all rejected"))
            continue
        points = _points(element.get("geometry"))
        if points is None:
            findings.append(OsmFinding("osm_way", key, "missing_geometry",
                                       "way has no finite lon/lat geometry"))
            continue
        if len(points) < 4 or points[0] != points[-1]:
            findings.append(OsmFinding("osm_way", key, "unclosed_way",
                                       f"{len(points)} points; first and last must match"))
            continue
        try:
            polygon = Polygon(points)
            check_wgs84(polygon)
            utm = to_projected(polygon)
        except (GeometryError, ValueError) as exc:
            findings.append(OsmFinding("osm_way", key, "invalid_geometry", str(exc)))
            continue
        if not shapely.is_valid(utm) or float(utm.area) <= 0:
            reason = str(shapely.is_valid_reason(utm)) if not shapely.is_valid(utm) else "zero area"
            findings.append(OsmFinding("osm_way", key, "invalid_geometry", reason))
            continue
        raw_tags = element.get("tags")
        tags: dict[str, str] = {}
        if isinstance(raw_tags, dict):
            for tag in OSM_TAGS:
                value = raw_tags.get(tag)
                if isinstance(value, str):
                    tags[tag] = value
        ways.append(OsmBuilding(key, utm, tags))
    return ways, findings


def _evidence(footprint_id: str, way: OsmBuilding, provenance: OsmProvenance) -> OsmEvidence:
    reference = f"{'test-only:' if provenance.test_only else ''}osm:{way.osm_id}"
    candidates: list[EvidenceCandidate] = []
    for tag, source in (("height", HeightSource.OSM_HEIGHT),
                        ("building:levels", HeightSource.OSM_LEVELS)):
        if tag in way.tags:
            candidates.append(EvidenceCandidate(
                source, way.tags[tag], reference=reference,
                source_text=f"{tag}={way.tags[tag]}", retrieved_at=provenance.retrieved_at))
    min_height = way.tags.get("min_height")
    return OsmEvidence(footprint_id, way.osm_id, tuple(candidates), min_height,
                       reference if min_height is not None else None, way.tags.get("building"),
                       provenance.licence, provenance.retrieved_at, provenance.test_only)


def join_osm(payload: Mapping[str, Any], provenance: OsmProvenance,
             footprints: Mapping[str, Any]) -> OsmJoinResult:
    """footprints: stable id -> EPSG:26910 geometry (PreparedFootprint.geom_utm)."""
    ways, findings = parse_ways(payload)
    elements = payload.get("elements")
    element_count = len(elements) if isinstance(elements, list) else 0
    ids = sorted(footprints)
    tree = shapely.STRtree([footprints[i] for i in ids]) if ids else None
    strong_by_way: dict[str, tuple[str, float, float]] = {}
    for way in ways:
        hits = [] if tree is None else sorted(
            ids[int(i)] for i in tree.query(way.geom_utm, predicate="intersects"))
        way_area = float(way.geom_utm.area)
        strong: list[tuple[str, float, float]] = []
        for fid in hits:
            footprint = footprints[fid]
            inter = float(way.geom_utm.intersection(footprint).area)
            fp_area = float(footprint.area)
            fp_ratio = inter / fp_area if fp_area > 0 else 0.0
            way_ratio = inter / way_area
            if fp_ratio >= STRONG_OVERLAP and way_ratio >= STRONG_OVERLAP:
                strong.append((fid, round(fp_ratio, 4), round(way_ratio, 4)))
        if not strong:
            code = "weak_overlap_only" if hits else "unmatched_osm_way"
            findings.append(OsmFinding("osm_way", way.osm_id, code,
                                       f"no footprint with >= {STRONG_OVERLAP:.0%} mutual overlap",
                                       tuple(hits)))
        elif len(strong) > 1:
            findings.append(OsmFinding("osm_way", way.osm_id, "ambiguous_osm_way",
                                       f"{len(strong)} footprints meet the overlap threshold",
                                       tuple(s[0] for s in strong)))
        else:
            strong_by_way[way.osm_id] = strong[0]
    ways_by_footprint: dict[str, list[str]] = defaultdict(list)
    for way_id, (fid, _, _) in sorted(strong_by_way.items()):
        ways_by_footprint[fid].append(way_id)
    by_id = {way.osm_id: way for way in ways}
    matches: list[OsmMatch] = []
    evidence: dict[str, OsmEvidence] = {}
    for fid, way_ids in sorted(ways_by_footprint.items()):
        if len(way_ids) > 1:
            findings.append(OsmFinding("footprint", fid, "conflicting_osm_ways",
                                       f"{len(way_ids)} OSM ways strongly overlap; none used",
                                       tuple(way_ids)))
            continue
        _, fp_ratio, way_ratio = strong_by_way[way_ids[0]]
        matches.append(OsmMatch(fid, way_ids[0], fp_ratio, way_ratio))
        evidence[fid] = _evidence(fid, by_id[way_ids[0]], provenance)
    return OsmJoinResult(provenance, element_count, len(ways), tuple(matches), evidence,
                         tuple(findings), tuple(fid for fid in ids if fid not in evidence))
