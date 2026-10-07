"""LOD1 prism extrusion and OBJ serialisation (pure; no rendering dependency).

Input geometry is EPSG:26910 metres. Vertices are written relative to one shared
local origin (E, N) so world = local + origin; axes x=east, y=north, z=up,
right-handed. Each polygon part is oriented (exterior CCW, holes CW seen from
above); for an edge a->b, walls (a0,b0,b1) and (a0,b1,a1) then face outward from the
solid, including into hole voids. Caps are triangulated by mapbox_earcut with holes;
each triangle is forced CCW for the top cap (+z) and reversed for the bottom (-z).
Holes are not filled. A simple n-vertex ring yields 2n wall and n-2 triangles per
cap; earcut may emit fewer cap triangles for collinear vertices (reported, not hidden).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import mapbox_earcut
import numpy as np
import numpy.typing as npt
import shapely
from shapely.geometry.polygon import orient

from massing.heights import EvidenceError, validate_building_id

MAX_VERTICES_PER_BUILDING = 200_000
MAX_TOTAL_VERTICES = 5_000_000
OBJ_DECIMALS = 3
Origin = tuple[float, float]


class ExtrusionError(ValueError):
    """Geometry or heights cannot be extruded, or limits/indices are violated."""


@dataclass(frozen=True, slots=True)
class Mesh:
    building_id: str
    origin: Origin
    vertices: npt.NDArray[np.float64]
    faces: npt.NDArray[np.int64]
    ring_sizes: tuple[tuple[int, ...], ...]
    wall_triangles: int
    cap_triangles: int
    expected_cap_triangles: int

    @property
    def part_count(self) -> int:
        return len(self.ring_sizes)

    @property
    def hole_count(self) -> int:
        return sum(len(r) - 1 for r in self.ring_sizes)


def shared_origin(geoms_utm: Iterable[Any]) -> Origin:
    """Floor of the minimum easting/northing over all geometries (whole metres)."""
    bounds = [shapely.bounds(g) for g in geoms_utm]
    if not bounds:
        raise ExtrusionError("cannot compute a shared origin without geometries")
    xs = [float(b[0]) for b in bounds]
    ys = [float(b[1]) for b in bounds]
    if not all(math.isfinite(v) for v in (*xs, *ys)):
        raise ExtrusionError("non-finite bounds")
    return (float(math.floor(min(xs))), float(math.floor(min(ys))))


def expected_cap_triangles(ring_sizes: Sequence[int]) -> int:
    """n + 2h - 2 for n total ring vertices and h holes."""
    return sum(ring_sizes) + 2 * (len(ring_sizes) - 1) - 2


def _rings(poly: Any) -> list[npt.NDArray[np.float64]]:
    oriented = orient(poly, sign=1.0)
    out: list[npt.NDArray[np.float64]] = []
    for ring in (oriented.exterior, *oriented.interiors):
        c = np.asarray(ring.coords, dtype=np.float64)[:, :2]
        keep = np.ones(len(c), dtype=bool)
        keep[1:] = np.any(np.diff(c, axis=0) != 0, axis=1)
        c = c[keep]
        if len(c) > 1 and np.array_equal(c[0], c[-1]):
            c = c[:-1]
        if len(c) < 3:
            raise ExtrusionError("ring has fewer than 3 distinct vertices")
        out.append(c)
    return out


def _earcut(points: npt.NDArray[np.float64], ends: npt.NDArray[np.uint32]
            ) -> npt.NDArray[np.int64]:
    raw = mapbox_earcut.triangulate_float64(np.ascontiguousarray(points), ends)
    tri = np.asarray(raw, dtype=np.int64).reshape(-1, 3)
    if tri.size == 0:
        raise ExtrusionError("earcut produced no cap triangles")
    return tri


def _part(poly: Any, z0: float, z1: float, origin: Origin, base: int
          ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.int64], tuple[int, ...], int, int]:
    rings = _rings(poly)
    sizes = tuple(len(r) for r in rings)
    pts = np.vstack(rings) - np.array(origin, dtype=np.float64)
    n = len(pts)
    verts = np.vstack((np.column_stack((pts, np.full(n, z0))),
                       np.column_stack((pts, np.full(n, z1)))))
    tri = _earcut(pts, np.cumsum(sizes).astype(np.uint32))
    a, b, c = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
    cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    cw = cross < 0
    tri[cw] = tri[cw][:, [0, 2, 1]]
    walls: list[npt.NDArray[np.int64]] = []
    start = 0
    for k in sizes:
        i = start + np.arange(k, dtype=np.int64)
        j = start + (np.arange(k, dtype=np.int64) + 1) % k
        walls.append(np.column_stack((i, j, j + n)))
        walls.append(np.column_stack((i, j + n, i + n)))
        start += k
    faces = np.vstack((tri[:, [0, 2, 1]], tri + n, *walls)) + base
    return verts, faces, sizes, 2 * n, len(tri)


def check_faces(faces: npt.NDArray[np.int64], n_vertices: int) -> None:
    if faces.ndim != 2 or faces.shape[1] != 3 or len(faces) == 0:
        raise ExtrusionError("faces must be a non-empty (F, 3) array")
    if int(faces.min()) < 0 or int(faces.max()) >= n_vertices:
        raise ExtrusionError("face index out of range")
    if np.any((faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2])
              | (faces[:, 0] == faces[:, 2])):
        raise ExtrusionError("degenerate face with repeated vertex index")


def extrude(building_id: str, geom_utm: Any, min_height_m: float, height_m: float,
            origin: Origin) -> Mesh:
    try:
        bid = validate_building_id(building_id)
    except EvidenceError as exc:
        raise ExtrusionError(str(exc)) from exc
    if not (math.isfinite(min_height_m) and math.isfinite(height_m)
            and 0 <= min_height_m < height_m):
        raise ExtrusionError(f"{bid}: need finite 0 <= min_height_m < height_m")
    if not all(math.isfinite(v) for v in origin):
        raise ExtrusionError(f"{bid}: origin must be finite")
    kind = getattr(geom_utm, "geom_type", None)
    if kind == "Polygon":
        polys = [geom_utm]
    elif kind == "MultiPolygon":
        polys = list(geom_utm.geoms)
    else:
        raise ExtrusionError(f"{bid}: cannot extrude {kind!r}")
    if geom_utm.is_empty or not shapely.is_valid(geom_utm):
        raise ExtrusionError(f"{bid}: geometry must be valid and non-empty")
    all_v: list[npt.NDArray[np.float64]] = []
    all_f: list[npt.NDArray[np.int64]] = []
    sizes: list[tuple[int, ...]] = []
    walls = caps = expected = base = 0
    for poly in polys:
        v, f, s, w, c = _part(poly, min_height_m, height_m, origin, base)
        base += len(v)
        if base > MAX_VERTICES_PER_BUILDING:
            raise ExtrusionError(f"{bid}: more than {MAX_VERTICES_PER_BUILDING} vertices")
        all_v.append(v)
        all_f.append(f)
        sizes.append(s)
        walls, caps, expected = walls + w, caps + c, expected + expected_cap_triangles(s)
    vertices = np.vstack(all_v)
    faces = np.vstack(all_f)
    check_faces(faces, len(vertices))
    return Mesh(bid, origin, vertices, faces, tuple(sizes), walls, caps, expected)


def obj_group_name(building_id: str) -> str:
    """Stable ids already match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}: no whitespace or '#'."""
    try:
        return validate_building_id(building_id)
    except EvidenceError as exc:
        raise ExtrusionError(str(exc)) from exc


def _fmt(value: float) -> str:
    return f"{round(float(value), OBJ_DECIMALS) + 0.0:.{OBJ_DECIMALS}f}"


def write_obj(meshes: Sequence[Mesh], origin: Origin, header: Sequence[str] = ()) -> str:
    """Deterministic OBJ text; one group per stable id; 1-based global indices."""
    names = [obj_group_name(m.building_id) for m in meshes]
    if len(set(names)) != len(names):
        raise ExtrusionError("duplicate building ids in OBJ")
    if any(m.origin != origin for m in meshes):
        raise ExtrusionError("all meshes must share the same local origin")
    if sum(len(m.vertices) for m in meshes) > MAX_TOTAL_VERTICES:
        raise ExtrusionError(f"more than {MAX_TOTAL_VERTICES} vertices in OBJ")
    if any("\n" in h or "\r" in h for h in header):
        raise ExtrusionError("header lines must be single-line")
    lines = [
        "# massing-provenance-kit LOD1 massing (not a survey model)",
        "# crs: EPSG:26910 (NAD83 / UTM zone 10N); units: metres",
        "# axes: x=east y=north z=up (right-handed); faces CCW seen from outside",
        f"# local_origin: E={_fmt(origin[0])} N={_fmt(origin[1])} Z=0.000 "
        "(world = local + origin)",
        *(f"# {h}" for h in header),
    ]
    offset = 1
    for name, mesh in zip(names, meshes, strict=True):
        check_faces(mesh.faces, len(mesh.vertices))
        lines.append(f"g {name}")
        lines.extend(f"v {_fmt(x)} {_fmt(y)} {_fmt(z)}" for x, y, z in mesh.vertices)
        lines.extend(f"f {a + offset} {b + offset} {c + offset}" for a, b, c in mesh.faces)
        offset += len(mesh.vertices)
    return "\n".join(lines) + "\n"


def parse_obj_counts(text: str) -> dict[str, tuple[int, int]]:
    """Group -> (vertex count, face count); validates indices refer to defined vertices."""
    counts: dict[str, list[int]] = {}
    group: str | None = None
    total = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        tag, _, rest = line.partition(" ")
        if tag == "g":
            if rest in counts:
                raise ExtrusionError(f"line {number}: duplicate group {rest!r}")
            group = rest
            counts[group] = [0, 0]
            continue
        if group is None:
            raise ExtrusionError(f"line {number}: data before first group")
        if tag == "v":
            counts[group][0] += 1
            total += 1
        elif tag == "f":
            idx = [int(token.split("/")[0]) for token in rest.split()]
            if len(idx) != 3 or any(i < 1 or i > total for i in idx):
                raise ExtrusionError(f"line {number}: invalid face indices")
            counts[group][1] += 1
        else:
            raise ExtrusionError(f"line {number}: unsupported OBJ record {tag!r}")
    return {name: (v, f) for name, (v, f) in counts.items()}
