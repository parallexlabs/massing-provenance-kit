"""Footprint preparation in EPSG:26910: validate, repair, simplify, drop slivers (pure).

Per footprint, every change or exclusion is reported, never silent:
1. accept GeoJSON mapping or shapely Polygon/MultiPolygon only; Z is dropped (reported);
2. WGS84 checks: finite, lon/lat in range, inside pyproj's area of use for EPSG:26910;
3. project to EPSG:26910; repair invalid geometry with shapely.make_valid keeping
   polygonal parts only (validity reason and dropped parts are reported);
4. simplify at 0.3 m with preserve_topology. The candidate is accepted only if it is
   valid (after an explicit, reported repair if needed), keeps the same number of
   polygon parts and holes, and changes area by at most 1% relative to the repaired
   pre-simplify geometry. Otherwise the pre-simplify geometry is retained and a
   simplification_rejected_* change is reported;
5. polygon parts < 10 m2 are dropped (reported); a footprint with no part >= 10 m2
   is excluded as a sliver; holes and remaining parts are preserved;
6. inverse-project the same metric geometry for MapLibre-ready WGS84 output; if the
   inverse result is empty, non-polygonal or invalid the footprint is excluded
   (wgs84_invalid_after_inverse) rather than repaired, keeping outputs consistent.
Width measure for QA: shorter side of the minimum rotated rectangle (metres).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

import numpy as np
import numpy.typing as npt
import shapely
from pyproj import CRS, Transformer
from shapely.errors import ShapelyError
from shapely.geometry import MultiPolygon, shape

from massing.heights import EvidenceError, validate_building_id

WGS84 = "EPSG:4326"
PROJECTED = "EPSG:26910"
SIMPLIFY_TOLERANCE_M = 0.3
MAX_SIMPLIFY_AREA_CHANGE = 0.01
SLIVER_AREA_M2 = 10.0
MAX_INPUT_VERTICES = 50_000
POLYGONAL = ("Polygon", "MultiPolygon")


class GeometryError(ValueError):
    """Unusable geometry; ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GeometryChange:
    building_id: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class Exclusion:
    building_id: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class PreparedFootprint:
    building_id: str
    geom_utm: Any
    geom_wgs84: Any
    area_m2: float
    input_area_m2: float
    pre_simplify_area_m2: float
    simplification_applied: bool
    input_vertices: int
    output_vertices: int
    part_count: int
    hole_count: int
    width_m: float
    changes: tuple[GeometryChange, ...]


@dataclass(frozen=True, slots=True)
class FootprintOutcome:
    footprint: PreparedFootprint | None
    exclusion: Exclusion | None
    changes: tuple[GeometryChange, ...]


@dataclass(frozen=True, slots=True)
class PreparedBatch:
    input_count: int
    footprints: tuple[PreparedFootprint, ...]
    exclusions: tuple[Exclusion, ...]
    changes: tuple[GeometryChange, ...]

    @property
    def reconciled(self) -> bool:
        return self.input_count == len(self.footprints) + len(self.exclusions)


@cache
def _transformer(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


@cache
def projected_area_of_use() -> tuple[float, float, float, float]:
    """(west, south, east, north) degrees, as reported by pyproj for EPSG:26910."""
    area = CRS.from_user_input(PROJECTED).area_of_use
    if area is None:
        raise RuntimeError("pyproj reports no area of use for EPSG:26910")
    return (float(area.west), float(area.south), float(area.east), float(area.north))


def _reproject(geom: Any, src: str, dst: str) -> Any:
    transformer = _transformer(src, dst)

    def apply(coords: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack((np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)))

    out = shapely.transform(geom, apply)
    if not np.isfinite(shapely.get_coordinates(out)).all():
        raise GeometryError("projection_failed", f"{src}->{dst} produced non-finite coordinates")
    return out


def to_projected(geom_wgs84: Any) -> Any:
    return _reproject(geom_wgs84, WGS84, PROJECTED)


def to_wgs84(geom_utm: Any) -> Any:
    return _reproject(geom_utm, PROJECTED, WGS84)


def check_wgs84(geom: Any) -> None:
    coords = shapely.get_coordinates(geom)
    if coords.size == 0:
        raise GeometryError("empty_geometry", "geometry has no coordinates")
    if not np.isfinite(coords).all():
        raise GeometryError("nonfinite_coordinates", "geometry has NaN or infinite coordinates")
    lon, lat = coords[:, 0], coords[:, 1]
    if (np.abs(lon) > 180).any() or (np.abs(lat) > 90).any():
        raise GeometryError("coordinates_out_of_range", "lon/lat outside WGS84 range")
    west, south, east, north = projected_area_of_use()
    if lon.min() < west or lon.max() > east or lat.min() < south or lat.max() > north:
        raise GeometryError("outside_crs_area_of_use",
                            f"outside EPSG:26910 area of use {west},{south},{east},{north}")


def parse_geometry(geometry: object) -> Any:
    """GeoJSON mapping or shapely geometry -> shapely geometry. Raises GeometryError."""
    if isinstance(geometry, Mapping):
        try:
            return shape(geometry)
        except (ShapelyError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise GeometryError("malformed_geometry", f"invalid GeoJSON geometry: {exc}") from exc
    if isinstance(geometry, shapely.Geometry):
        return geometry
    raise GeometryError("malformed_geometry", f"unsupported geometry object {type(geometry)!r}")


def polygon_parts(geom: Any) -> tuple[list[Any], list[str]]:
    """Polygons in deterministic order plus geometry types of dropped non-polygonal parts."""
    if geom.is_empty:
        return [], []
    if geom.geom_type == "Polygon":
        return [geom], []
    if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        polys: list[Any] = []
        dropped: list[str] = []
        for part in geom.geoms:
            p, d = polygon_parts(part)
            polys.extend(p)
            dropped.extend(d)
        return polys, dropped
    return [], [str(geom.geom_type)]


def _join(parts: list[Any]) -> Any:
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def repair(geom: Any) -> tuple[Any, list[str]]:
    """Explicit make_valid repair keeping polygonal area only."""
    parts, dropped = polygon_parts(shapely.make_valid(geom))
    if not parts:
        raise GeometryError("unrepairable_geometry", "repair produced no polygonal area")
    return _join(parts), dropped


def hole_count(geom: Any) -> int:
    parts, _ = polygon_parts(geom)
    return sum(len(p.interiors) for p in parts)


def _topology(geom: Any) -> tuple[int, int]:
    return len(polygon_parts(geom)[0]), hole_count(geom)


def footprint_width_m(geom_utm: Any) -> float:
    """Shorter side of the minimum rotated rectangle, metres (QA width measure)."""
    rect = shapely.minimum_rotated_rectangle(geom_utm)
    if rect.geom_type != "Polygon":
        return 0.0
    coords = np.asarray(rect.exterior.coords, dtype=np.float64)
    edges = np.hypot(np.diff(coords[:, 0]), np.diff(coords[:, 1]))
    return float(min(edges[0], edges[1]))


def _pct(new: float, old: float) -> float:
    return 100.0 * (new - old) / old


def simplify_guarded(bid: str, pre: Any) -> tuple[Any, bool, list[GeometryChange]]:
    """Simplify a valid metric geometry; reject (retain ``pre``) on >1% area or topology change."""
    changes: list[GeometryChange] = []
    pre_area = float(pre.area)
    pre_topo = _topology(pre)
    n_pre = int(shapely.get_num_coordinates(pre))
    candidate = shapely.simplify(pre, SIMPLIFY_TOLERANCE_M, preserve_topology=True)
    repair_note = ""
    if not shapely.is_valid(candidate):
        reason = str(shapely.is_valid_reason(candidate))
        try:
            candidate, dropped = repair(candidate)
        except GeometryError as exc:
            changes.append(GeometryChange(bid, "simplification_rejected_invalid",
                                          f"{reason}; {exc}; pre-simplify geometry retained"))
            return pre, False, changes
        repair_note = f"{reason}; dropped parts {dropped}"
    cand_area = float(candidate.area)
    cand_topo = _topology(candidate)
    if pre_area <= 0 or abs(cand_area - pre_area) / pre_area > MAX_SIMPLIFY_AREA_CHANGE:
        pct = _pct(cand_area, pre_area) if pre_area > 0 else float("nan")
        changes.append(GeometryChange(
            bid, "simplification_rejected_area_change",
            f"area {pre_area:.3f}->{cand_area:.3f} m2 ({pct:+.3f}%) exceeds "
            f"{100 * MAX_SIMPLIFY_AREA_CHANGE:.0f}%; pre-simplify geometry retained"
            + (f"; candidate repair {repair_note}" if repair_note else "")))
        return pre, False, changes
    if cand_topo != pre_topo:
        changes.append(GeometryChange(
            bid, "simplification_rejected_topology_change",
            f"parts/holes {pre_topo}->{cand_topo}; pre-simplify geometry retained"))
        return pre, False, changes
    if repair_note:
        changes.append(GeometryChange(bid, "repaired_after_simplify", repair_note))
    n_out = int(shapely.get_num_coordinates(candidate))
    if n_out != n_pre or abs(cand_area - pre_area) > 1e-9:
        changes.append(GeometryChange(
            bid, "simplified",
            f"vertices {n_pre}->{n_out}; area {pre_area:.3f}->{cand_area:.3f} m2 "
            f"({_pct(cand_area, pre_area):+.3f}%)"))
    return candidate, True, changes


def prepare_footprint(building_id: object, geometry: object) -> FootprintOutcome:
    try:
        bid = validate_building_id(building_id)
    except EvidenceError as exc:
        return FootprintOutcome(None, Exclusion(repr(building_id)[:130], exc.code, str(exc)), ())
    changes: list[GeometryChange] = []
    try:
        geom = parse_geometry(geometry)
        if geom.is_empty:
            raise GeometryError("empty_geometry", "geometry is empty")
        if geom.geom_type not in POLYGONAL:
            raise GeometryError("unsupported_geometry", f"{geom.geom_type} is not a footprint")
        n_in = int(shapely.get_num_coordinates(geom))
        if n_in > MAX_INPUT_VERTICES:
            raise GeometryError("too_many_vertices", f"{n_in} > {MAX_INPUT_VERTICES} vertices")
        if shapely.has_z(geom):
            geom = shapely.force_2d(geom)
            changes.append(GeometryChange(bid, "z_dropped", "Z coordinates removed"))
        check_wgs84(geom)
        utm = to_projected(geom)
        input_area = float(utm.area)
        parts_in, holes_in = _topology(utm)
        if not shapely.is_valid(utm):
            reason = str(shapely.is_valid_reason(utm))
            utm, dropped = repair(utm)
            changes.append(GeometryChange(
                bid, "repaired",
                f"{reason}; dropped parts {dropped}; area {input_area:.3f}->"
                f"{float(utm.area):.3f} m2"))
        pre_area = float(utm.area)
        if pre_area <= 0:
            raise GeometryError("sliver", "repaired footprint has zero area")
        simplified, applied, simplify_changes = simplify_guarded(bid, utm)
        changes.extend(simplify_changes)
        parts, _ = polygon_parts(simplified)
        kept = [p for p in parts if float(p.area) >= SLIVER_AREA_M2]
        for part in parts:
            if float(part.area) < SLIVER_AREA_M2:
                changes.append(GeometryChange(bid, "sliver_part_dropped",
                                              f"part area {float(part.area):.3f} m2 < 10 m2"))
        if not kept:
            raise GeometryError("sliver", f"area {float(simplified.area):.3f} m2 < 10 m2")
        final = _join(kept)
        area = float(final.area)
        n_out = int(shapely.get_num_coordinates(final))
        holes_out = hole_count(final)
        if holes_out != holes_in or len(kept) != parts_in:
            changes.append(GeometryChange(bid, "topology_changed",
                                          f"parts {parts_in}->{len(kept)}; "
                                          f"holes {holes_in}->{holes_out}"))
        wgs = to_wgs84(final)
        if wgs.is_empty or wgs.geom_type not in POLYGONAL or not shapely.is_valid(wgs):
            reason = "empty" if wgs.is_empty else str(shapely.is_valid_reason(wgs))
            raise GeometryError("wgs84_invalid_after_inverse",
                                f"inverse-projected {wgs.geom_type} unusable: {reason}")
        prepared = PreparedFootprint(bid, final, wgs, area, input_area, pre_area, applied, n_in,
                                     n_out, len(kept), holes_out, footprint_width_m(final),
                                     tuple(changes))
        return FootprintOutcome(prepared, None, tuple(changes))
    except GeometryError as exc:
        return FootprintOutcome(None, Exclusion(bid, exc.code, str(exc)), tuple(changes))


def prepare_footprints(items: Iterable[tuple[object, object]]) -> PreparedBatch:
    """Prepare all footprints; duplicated ids are all excluded. Output sorted by id."""
    rows = list(items)
    counts = Counter(bid for bid, _ in rows if isinstance(bid, str))
    footprints: list[PreparedFootprint] = []
    exclusions: list[Exclusion] = []
    changes: list[GeometryChange] = []
    for bid, geometry in rows:
        if isinstance(bid, str) and counts[bid] > 1:
            exclusions.append(Exclusion(bid, "duplicate_id", f"id occurs {counts[bid]} times"))
            continue
        outcome = prepare_footprint(bid, geometry)
        changes.extend(outcome.changes)
        if outcome.footprint is not None:
            footprints.append(outcome.footprint)
        elif outcome.exclusion is not None:
            exclusions.append(outcome.exclusion)
    return PreparedBatch(
        len(rows),
        tuple(sorted(footprints, key=lambda f: f.building_id)),
        tuple(sorted(exclusions, key=lambda e: (e.building_id, e.code))),
        tuple(sorted(changes, key=lambda c: (c.building_id, c.code, c.detail))),
    )
