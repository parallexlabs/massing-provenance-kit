"""Self-contained glTF 2.0 schematic prisms for validated csv2massing buildings (pure).

Reuses extrude.shared_origin and extrude.extrude unchanged on the projected EPSG:26910 copy
of the original footprint, so polygon holes and MultiPolygon parts are preserved. Mesh axes
east, north, up map to glTF +X east, +Y up, +Z south (negative north) via
(x, y, z) -> (x, z, -y). That is a proper rotation (determinant +1), so triangle winding and
outward-facing walls/caps are preserved. Positions are little-endian float32 VEC3 and indices
little-endian uint32 in one base64 data-URI buffer, each view 4-byte aligned; accessor
min/max come from the stored float32 values. Every stored triangle must have strictly
positive geometric area, computed in float64 from the decoded float32 positions; a model
whose float32 positions collapse (e.g. a positive height too small to represent) or cannot
be represented is rejected as invalid_model, never emitted with zero or invented height.
The per-building origin is the floored minimum easting/northing in whole metres; the
EPSG:26910 origin and its WGS84 anchor are recorded in extras. A single schematic prism of
the supplied total height: no podium addition, no materials, textures or facade detail, and
not a detailed architectural model.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
from shapely.geometry import Point

from massing import __version__
from massing.csvvalidate import Building
from massing.extrude import ExtrusionError, extrude, shared_origin
from massing.simplify import GeometryError, to_wgs84

FLOAT = 5126
UNSIGNED_INT = 5125
ARRAY_BUFFER = 34962
ELEMENT_ARRAY_BUFFER = 34963
TRIANGLES = 4
DATA_URI_PREFIX = "data:application/octet-stream;base64,"
AXES = "glTF +X = east, +Y = up, +Z = south (negative north); metres relative to origin"
SCHEMATIC_NOTE = ("schematic single prism of the supplied total height; not a detailed "
                  "architectural model")


class GltfError(ValueError):
    """A model cannot be produced or fails self-validation; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GltfModel:
    building_id: str
    text: str
    sha256: str
    vertex_count: int
    triangle_count: int
    wall_triangles: int
    cap_triangles: int
    expected_cap_triangles: int
    part_count: int
    hole_count: int
    origin: tuple[float, float]
    wgs84_anchor: tuple[float, float]

    def summary(self) -> dict[str, object]:
        return {
            "building_id": self.building_id, "sha256": self.sha256,
            "vertex_count": self.vertex_count, "triangle_count": self.triangle_count,
            "wall_triangles": self.wall_triangles, "cap_triangles_per_cap": self.cap_triangles,
            "expected_cap_triangles_per_cap": self.expected_cap_triangles,
            "part_count": self.part_count, "hole_count": self.hole_count,
            "origin_epsg26910": list(self.origin), "wgs84_anchor": list(self.wgs84_anchor),
        }


def _topology(geom: Any) -> tuple[int, int]:
    parts = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    return len(parts), sum(len(p.interiors) for p in parts)


def _positions(vertices: npt.NDArray[np.float64]) -> npt.NDArray[np.float32]:
    converted = np.column_stack((vertices[:, 0], vertices[:, 2], -vertices[:, 1]))
    try:
        with np.errstate(over="raise", invalid="raise"):
            stored = converted.astype("<f4") + np.float32(0.0)
    except FloatingPointError as exc:
        raise GltfError("invalid_model",
                        "mesh positions cannot be represented as float32") from exc
    if not np.isfinite(stored).all():
        raise GltfError("invalid_model", "mesh has non-finite positions")
    return np.ascontiguousarray(stored, dtype="<f4")


def _zero_area_triangles(positions: npt.NDArray[np.float32],
                         indices: npt.NDArray[np.uint32]) -> int:
    """Count triangles whose geometric area (float64, from stored float32) is not > 0."""
    corners = positions.astype(np.float64)[indices.astype(np.int64)]
    with np.errstate(over="raise", invalid="raise", under="ignore"):
        normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
        doubled = np.sqrt(np.einsum("ij,ij->i", normals, normals))
    return int(np.count_nonzero(~(np.isfinite(doubled) & (doubled > 0.0))))


def check_gltf(text: str) -> tuple[int, int]:
    """Re-read a model: buffer length, alignment, accessor bounds/lengths, finite positions,
    index range, distinct triangle indices and strictly positive triangle areas computed in
    float64 from the stored float32 positions. Returns (vertex_count, triangle_count)."""
    try:
        doc = json.loads(text)
        buffer = doc["buffers"][0]
        uri = buffer["uri"]
        if not isinstance(uri, str) or not uri.startswith(DATA_URI_PREFIX):
            raise GltfError("invalid_model", "buffer is not an embedded base64 data URI")
        data = base64.b64decode(uri[len(DATA_URI_PREFIX):], validate=True)
        if len(data) != buffer["byteLength"]:
            raise GltfError("invalid_model", "buffer byteLength differs from decoded data")
        pos_acc, idx_acc = doc["accessors"]
        views = doc["bufferViews"]
        pos_view, idx_view = views[pos_acc["bufferView"]], views[idx_acc["bufferView"]]
        for view in (pos_view, idx_view):
            if view["byteOffset"] % 4 or view["byteOffset"] + view["byteLength"] > len(data):
                raise GltfError("invalid_model", "buffer view is misaligned or out of range")
        count = pos_acc["count"]
        if (pos_acc["componentType"], pos_acc["type"]) != (FLOAT, "VEC3") \
                or pos_view["byteLength"] != count * 12 or count < 3:
            raise GltfError("invalid_model", "POSITION accessor does not match its view")
        positions = np.frombuffer(data, dtype="<f4", count=count * 3,
                                  offset=pos_view["byteOffset"]).reshape(-1, 3)
        if not np.isfinite(positions).all():
            raise GltfError("invalid_model", "stored positions are not finite")
        if [float(v) for v in positions.min(axis=0)] != pos_acc["min"] \
                or [float(v) for v in positions.max(axis=0)] != pos_acc["max"]:
            raise GltfError("invalid_model", "POSITION min/max differ from stored floats")
        icount = idx_acc["count"]
        if (idx_acc["componentType"], idx_acc["type"]) != (UNSIGNED_INT, "SCALAR") \
                or icount == 0 or icount % 3 or idx_view["byteLength"] != icount * 4:
            raise GltfError("invalid_model", "index accessor does not match its view")
        indices = np.frombuffer(data, dtype="<u4", count=icount,
                                offset=idx_view["byteOffset"]).reshape(-1, 3)
        if int(indices.max()) >= count:
            raise GltfError("invalid_model", "index refers to a missing vertex")
        if np.any((indices[:, 0] == indices[:, 1]) | (indices[:, 1] == indices[:, 2])
                  | (indices[:, 0] == indices[:, 2])):
            raise GltfError("invalid_model", "degenerate triangle with repeated index")
        zero_area = _zero_area_triangles(positions, indices)
        if zero_area:
            raise GltfError("invalid_model", f"{zero_area} triangle(s) have zero geometric "
                            "area in the stored float32 positions; the model is not "
                            "representable and is rejected")
        if doc["meshes"][0]["primitives"][0].get("mode", TRIANGLES) != TRIANGLES:
            raise GltfError("invalid_model", "primitive is not triangles")
    except GltfError:
        raise
    except FloatingPointError as exc:
        raise GltfError("invalid_model", f"triangle area is not computable: {exc}") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise GltfError("invalid_model", f"model structure is invalid: {exc}") from exc
    return int(count), len(indices)


def build_gltf(building: Building, provenance_file: str) -> GltfModel:
    """Build and self-check one embedded glTF 2.0 prism; raises GltfError, never guesses."""
    height = building.height_m
    if height is None:
        raise GltfError("missing_height", f"{building.id}: height is unknown; no model")
    fp = building.footprint
    try:
        origin = shared_origin([fp.geom_utm])
        mesh = extrude(building.id, fp.geom_utm, 0.0, height, origin)
    except ExtrusionError as exc:
        raise GltfError("extrusion_failed", str(exc)) from exc
    parts, holes = _topology(fp.geom_utm)
    if mesh.part_count != parts or mesh.hole_count != holes:
        raise GltfError("extrusion_failed", "mesh lost polygon parts or holes")
    ring_vertices = sum(sum(sizes) for sizes in mesh.ring_sizes)
    if mesh.cap_triangles < 1 or mesh.wall_triangles != 2 * ring_vertices \
            or len(mesh.faces) != 2 * mesh.cap_triangles + mesh.wall_triangles:
        raise GltfError("extrusion_failed", "mesh roof/floor/wall triangle counts are inconsistent")
    try:
        anchor_point = to_wgs84(Point(origin))
    except GeometryError as exc:
        raise GltfError("extrusion_failed", f"origin cannot be anchored: {exc}") from exc
    anchor = (float(anchor_point.x), float(anchor_point.y))
    positions = _positions(mesh.vertices)
    faces = mesh.faces
    if int(faces.min()) < 0 or int(faces.max()) >= len(positions):
        raise GltfError("invalid_model", "face index out of range")
    indices = np.ascontiguousarray(faces.reshape(-1), dtype="<u4")
    pos_bytes, idx_bytes = positions.tobytes(), indices.tobytes()
    data = pos_bytes + idx_bytes
    extras: dict[str, object] = {
        "building_id": building.id, "physical_building_id": fp.physical_id,
        "provenance_file": provenance_file, "schematic": True, "detailed": False,
        "test_only": building.provenance.test_only, "height_m": height, "base_height_m": 0.0,
        "geometry_sha256": fp.geometry_sha256, "units": "metre", "axes": AXES,
        "origin": {"crs": "EPSG:26910", "easting_m": origin[0], "northing_m": origin[1],
                   "elevation_m": 0.0},
        "wgs84_anchor": {"crs": "EPSG:4326", "longitude": anchor[0], "latitude": anchor[1],
                         "note": "origin inverse-projected; elevation 0 m is the extrusion "
                                 "base, not a surveyed vertical datum"},
        "note": SCHEMATIC_NOTE,
    }
    doc: dict[str, object] = {
        "asset": {"version": "2.0", "generator": f"csv2massing {__version__}"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": building.id}],
        "meshes": [{"name": building.id, "primitives": [
            {"attributes": {"POSITION": 0}, "indices": 1, "mode": TRIANGLES}]}],
        "accessors": [
            {"bufferView": 0, "componentType": FLOAT, "count": len(positions), "type": "VEC3",
             "min": [float(v) for v in positions.min(axis=0)],
             "max": [float(v) for v in positions.max(axis=0)]},
            {"bufferView": 1, "componentType": UNSIGNED_INT, "count": len(indices),
             "type": "SCALAR", "min": [int(indices.min())], "max": [int(indices.max())]}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes), "target": ARRAY_BUFFER},
            {"buffer": 0, "byteOffset": len(pos_bytes), "byteLength": len(idx_bytes),
             "target": ELEMENT_ARRAY_BUFFER}],
        "buffers": [{"byteLength": len(data),
                     "uri": DATA_URI_PREFIX + base64.b64encode(data).decode("ascii")}],
        "extras": extras,
    }
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)
    vertex_count, triangle_count = check_gltf(text)
    return GltfModel(building.id, text, hashlib.sha256(text.encode("utf-8")).hexdigest(),
                     vertex_count, triangle_count, mesh.wall_triangles, mesh.cap_triangles,
                     mesh.expected_cap_triangles, parts, holes, origin, anchor)
