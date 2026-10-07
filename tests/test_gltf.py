"""Independent decoding and geometry checks for offline schematic glTF models."""

import base64
import hashlib
import json
import math
import struct
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from massing.csvio import read_csv
from massing.csvvalidate import validate
from massing.gltf import GltfError, build_gltf, check_gltf

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/csv2massing/synthetic"
PREFIX = "data:application/octet-stream;base64,"


@pytest.fixture
def buildings():
    result = validate(read_csv(EXAMPLE / "buildings.csv"),
                      json.loads((EXAMPLE / "footprints.geojson").read_text()),
                      json.loads((EXAMPLE / "provenance-manifest.json").read_text()))
    assert result.ok
    return {building.id: building for building in result.buildings}


def decode(text):
    doc = json.loads(text)
    buffer, = doc["buffers"]
    assert buffer["uri"].startswith(PREFIX)
    data = bytearray(base64.b64decode(buffer["uri"].split(",", 1)[1], validate=True))
    assert len(data) == buffer["byteLength"]
    primitive = doc["meshes"][0]["primitives"][0]
    position = doc["accessors"][primitive["attributes"]["POSITION"]]
    index = doc["accessors"][primitive["indices"]]
    pview, iview = doc["bufferViews"][position["bufferView"]], doc["bufferViews"][index["bufferView"]]
    assert (position["componentType"], position["type"]) == (5126, "VEC3")
    assert (index["componentType"], index["type"]) == (5125, "SCALAR")
    for view in (pview, iview):
        assert view["buffer"] == 0 and view["byteOffset"] % 4 == 0
        assert view["byteOffset"] + view["byteLength"] <= len(data)
    assert pview["byteLength"] == position["count"] * 12
    assert iview["byteLength"] == index["count"] * 4
    poffset = pview["byteOffset"] + position.get("byteOffset", 0)
    ioffset = iview["byteOffset"] + index.get("byteOffset", 0)
    positions = np.frombuffer(data, dtype="<f4", count=position["count"] * 3, offset=poffset).reshape(-1, 3)
    indices = np.frombuffer(data, dtype="<u4", count=index["count"], offset=ioffset).reshape(-1, 3)
    assert np.isfinite(positions).all() and indices.max() < len(positions)
    assert position["min"] == positions.min(axis=0).astype(float).tolist()
    assert position["max"] == positions.max(axis=0).astype(float).tolist()
    assert index["min"] == [int(indices.min())] and index["max"] == [int(indices.max())]
    return doc, data, positions, indices, poffset, ioffset


@pytest.mark.parametrize("fid,height,parts,holes,vertices,walls,caps", [
    ("synthetic-01", 24, 1, 0, 8, 8, 2),
    ("synthetic-03", 18.5, 1, 1, 16, 16, 8),
    ("synthetic-04", 20.5, 2, 0, 16, 16, 4),
])
def test_prism_binary_axes_caps_holes_walls_and_provenance(buildings, fid, height, parts, holes, vertices, walls, caps):
    building = buildings[fid]
    pointer = f"provenance/{fid}.json"
    model = build_gltf(building, pointer)
    assert build_gltf(building, pointer).text == model.text
    assert model.sha256 == hashlib.sha256(model.text.encode()).hexdigest()
    doc, _, positions, indices, _, _ = decode(model.text)
    assert doc["asset"]["version"] == "2.0" and doc["meshes"][0]["primitives"][0]["mode"] == 4
    assert len(positions) == model.vertex_count == vertices
    assert len(indices) == model.triangle_count == walls + 2 * caps
    assert (model.part_count, model.hole_count, model.wall_triangles, model.cap_triangles) == (parts, holes, walls, caps)
    assert check_gltf(model.text) == (vertices, walls + 2 * caps)
    assert positions[:, 1].min() == 0 and positions[:, 1].max() == height
    extras = doc["extras"]
    assert extras["building_id"] == fid and extras["physical_building_id"] == building.footprint.physical_id
    assert extras["provenance_file"] == pointer and extras["test_only"] is True
    assert extras["schematic"] is True and extras["detailed"] is False and extras["units"] == "metre"
    assert "+X = east" in extras["axes"] and "+Y = up" in extras["axes"] and "+Z = south" in extras["axes"]
    geom = building.footprint.geom_utm
    origin = (math.floor(geom.bounds[0]), math.floor(geom.bounds[1]))
    assert model.origin == origin and extras["origin"]["crs"] == "EPSG:26910"
    assert (extras["origin"]["easting_m"], extras["origin"]["northing_m"], extras["origin"]["elevation_m"]) == (*origin, 0)
    anchor = extras["wgs84_anchor"]
    expected_anchor = Transformer.from_crs(26910, 4326, always_xy=True).transform(*origin)
    assert anchor["crs"] == "EPSG:4326" and model.wgs84_anchor == pytest.approx(expected_anchor, abs=1e-8)
    assert (anchor["longitude"], anchor["latitude"]) == pytest.approx(expected_anchor, abs=1e-8)
    digest = hashlib.sha256(json.dumps(building.footprint.geometry, sort_keys=True,
                                      separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert extras["geometry_sha256"] == digest
    assert model.summary()["origin_epsg26910"] == list(origin)
    triangles = positions.astype(np.float64)[indices]
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    assert np.all(np.linalg.norm(normals, axis=1) > 0)
    horizontal = np.ptp(triangles[:, :, 1], axis=1) == 0
    roof = horizontal & (triangles[:, 0, 1] == height)
    floor = horizontal & (triangles[:, 0, 1] == 0)
    assert roof.sum() == floor.sum() == caps and (~horizontal).sum() == walls
    assert np.all(normals[roof, 1] > 0) and np.all(normals[floor, 1] < 0)
    assert np.all(normals[~horizontal, 1] == 0)
    world_xy = np.column_stack((positions[:, 0].astype(float) + origin[0],
                               -positions[:, 2].astype(float) + origin[1]))
    assert (world_xy[:, 0].min(), world_xy[:, 1].min(), world_xy[:, 0].max(),
            world_xy[:, 1].max()) == pytest.approx(geom.bounds, abs=1e-4)
    for mask in (roof, floor):
        polygons = [Polygon(world_xy[face]) for face in indices[mask]]
        assert sum(p.area for p in polygons) == pytest.approx(geom.area, rel=1e-5, abs=.02)
        assert unary_union(polygons).symmetric_difference(geom).area < .02
    for face, normal in zip(indices[~horizontal], normals[~horizontal], strict=True):
        center = world_xy[face].mean(axis=0)
        direction = np.array([normal[0], -normal[2]])
        direction /= np.linalg.norm(direction)
        assert not geom.covers(Point(center + direction * .001))
        assert geom.covers(Point(center - direction * .001))


@pytest.mark.filterwarnings("error::RuntimeWarning")
@pytest.mark.parametrize("height,code", [(None, "missing_height"), (1e-300, "invalid_model"), (1e39, "invalid_model")])
def test_unknown_and_unrepresentable_heights_raise_clean_errors(buildings, height, code):
    building = buildings["synthetic-01"]
    value = replace(building.row.height, known=height is not None, height_m=height)
    building = replace(building, row=replace(building.row, height=value))
    with pytest.raises(GltfError) as caught:
        build_gltf(building, "provenance/synthetic-01.json")
    assert caught.value.code == code and str(caught.value) and "Traceback" not in str(caught.value)


@pytest.mark.parametrize("text", ["{broken", "{}"])
def test_checker_wraps_json_and_structure_failures(text):
    with pytest.raises(GltfError) as caught:
        check_gltf(text)
    assert caught.value.code == "invalid_model"


@pytest.mark.parametrize("case", [
    "external_uri", "bad_base64", "buffer_length", "alignment", "position_type",
    "index_type", "accessor_length", "position_bounds", "nonfinite", "out_of_range",
    "repeated_indices", "primitive_mode", "zero_geometric_area",
])
def test_tampered_serialized_models_rejected_by_documented_checker(buildings, case):
    text = build_gltf(buildings["synthetic-01"], "provenance/synthetic-01.json").text
    doc, data, positions, indices, poffset, ioffset = decode(text)
    if case == "external_uri":
        doc["buffers"][0]["uri"] = "external.bin"
    elif case == "bad_base64":
        doc["buffers"][0]["uri"] = PREFIX + "!not-base64!"
    elif case == "buffer_length":
        doc["buffers"][0]["byteLength"] += 1
    elif case == "alignment":
        doc["bufferViews"][0]["byteOffset"] = 1
    elif case == "position_type":
        doc["accessors"][0]["type"] = "VEC2"
    elif case == "index_type":
        doc["accessors"][1]["componentType"] = 5123
    elif case == "accessor_length":
        doc["accessors"][1]["count"] -= 1
    elif case == "position_bounds":
        doc["accessors"][0]["max"][0] += 1
    elif case == "nonfinite":
        struct.pack_into("<f", data, poffset, float("inf"))
    elif case == "out_of_range":
        struct.pack_into("<I", data, ioffset, len(positions))
    elif case == "repeated_indices":
        struct.pack_into("<I", data, ioffset + 4, int(indices[0, 0]))
    elif case == "primitive_mode":
        doc["meshes"][0]["primitives"][0]["mode"] = 1
    else:
        face = indices[0].copy()
        assert len(set(map(int, face))) == 3
        positions[face] = [[0, 0, 0], [1, 0, 0], [2, 0, 0]]
        doc["accessors"][0]["min"] = positions.min(axis=0).astype(float).tolist()
        doc["accessors"][0]["max"] = positions.max(axis=0).astype(float).tolist()
        assert len(set(map(int, indices[0]))) == 3
    if case not in {"external_uri", "bad_base64"}:
        doc["buffers"][0]["uri"] = PREFIX + base64.b64encode(data).decode("ascii")
    with pytest.raises(GltfError) as caught:
        check_gltf(json.dumps(doc, allow_nan=False))
    assert caught.value.code == "invalid_model" and str(caught.value)
    if case == "zero_geometric_area":
        assert "area" in str(caught.value)
