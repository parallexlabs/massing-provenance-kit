"""Synthetic metre-coordinate meshes; no renderer, network, or live evidence."""

from collections import Counter

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

import massing.extrude as extrusion
from massing.extrude import (
    ExtrusionError,
    check_faces,
    expected_cap_triangles,
    extrude,
    obj_group_name,
    parse_obj_counts,
    shared_origin,
    write_obj,
)

E, N = 510000.25, 5450000.75


def geometries():
    square = box(E, N, E + 20, N + 20)
    hole = box(E + 5, N + 5, E + 15, N + 15)
    courtyard = Polygon(square.exterior.coords, [hole.exterior.coords])
    concave = Polygon([(E, N), (E + 20, N), (E + 20, N + 10),
                       (E + 10, N + 10), (E + 10, N + 20), (E, N + 20)])
    return [square, courtyard, MultiPolygon([courtyard, box(E + 30, N, E + 40, N + 10)]),
            concave, Polygon(list(square.exterior.coords)[::-1])]


@pytest.mark.parametrize("geometry", geometries())
def test_counts_indices_caps_normals_and_closed_surface(geometry):
    origin = shared_origin([geometry])
    mesh = extrude("synthetic", geometry, 2, 32.5, origin)
    n = sum(sum(rings) for rings in mesh.ring_sizes)
    expected = sum(expected_cap_triangles(rings) for rings in mesh.ring_sizes)
    assert len(mesh.vertices) == 2 * n
    assert mesh.wall_triangles == 2 * n
    assert mesh.cap_triangles == mesh.expected_cap_triangles == expected
    assert len(mesh.faces) == 2 * expected + 2 * n
    assert np.isfinite(mesh.vertices).all()
    assert mesh.faces.min() >= 0 and mesh.faces.max() < len(mesh.vertices)
    assert set(mesh.vertices[:, 2]) == {2, 32.5}
    world = mesh.vertices + np.array([*origin, 0])
    triangles = world[mesh.faces]
    normals = np.cross(triangles[:, 1] - triangles[:, 0],
                       triangles[:, 2] - triangles[:, 0])
    assert np.all(np.linalg.norm(normals, axis=1) > 0)
    caps = {2: [], 32.5: []}
    wall_count = 0
    for triangle, normal in zip(triangles, normals, strict=True):
        if np.all(triangle[:, 2] == triangle[0, 2]):
            z = triangle[0, 2]
            assert normal[2] < 0 if z == 2 else normal[2] > 0
            caps[z].append(Polygon(triangle[:, :2]))
        else:
            wall_count += 1
            assert abs(normal[2]) < 1e-8
            center = triangle[:, :2].mean(axis=0)
            direction = normal[:2] / np.linalg.norm(normal[:2])
            assert not geometry.covers(Point(center + direction * .001))
            assert geometry.covers(Point(center - direction * .001))
    assert wall_count == mesh.wall_triangles
    for cap in caps.values():
        assert len(cap) == expected
        assert sum(part.area for part in cap) == pytest.approx(geometry.area, abs=1e-6)
        assert unary_union(cap).symmetric_difference(geometry).area < 1e-6
    edges = Counter(tuple(sorted((int(a), int(b)))) for face in mesh.faces
                    for a, b in zip(face, np.roll(face, -1), strict=True))
    assert set(edges.values()) == {2}
    signed_volume = np.einsum("ij,ij->i", mesh.vertices[mesh.faces[:, 0]],
                             np.cross(mesh.vertices[mesh.faces[:, 1]],
                                      mesh.vertices[mesh.faces[:, 2]])).sum() / 6
    assert signed_volume == pytest.approx(geometry.area * 30.5, rel=1e-9)


def test_simple_prism_exact_counts_and_multipart_holes():
    square, courtyard, multipart = geometries()[:3]
    mesh = extrude("b1", square, 0, 10, shared_origin([square]))
    assert (len(mesh.vertices), mesh.wall_triangles, mesh.cap_triangles) == (8, 8, 2)
    assert len(mesh.faces) == 12
    mesh = extrude("b3", courtyard, 0, 10, shared_origin([courtyard]))
    assert (mesh.part_count, mesh.hole_count, mesh.wall_triangles, mesh.cap_triangles) == (
        1, 1, 16, 8,
    )
    mesh = extrude("b4", multipart, 0, 10, shared_origin([multipart]))
    assert (mesh.part_count, mesh.hole_count, mesh.wall_triangles, mesh.cap_triangles) == (
        2, 1, 24, 10,
    )


def test_obj_round_trip_global_indices_groups_and_shared_origin():
    shapes = geometries()[:3]
    origin = shared_origin(shapes)
    assert origin == (510000, 5450000)
    meshes = [extrude(f"b{i}", geometry, 2, 10, origin)
              for i, geometry in enumerate(shapes, start=1)]
    text = write_obj(meshes, origin, header=("TEST ONLY: synthetic geometry",))
    assert text == write_obj(meshes, origin, header=("TEST ONLY: synthetic geometry",))
    assert "EPSG:26910" in text and "units: metres" in text
    assert "x=east y=north z=up" in text and "E=510000.000 N=5450000.000" in text
    assert parse_obj_counts(text) == {
        mesh.building_id: (len(mesh.vertices), len(mesh.faces)) for mesh in meshes
    }
    vertices, faces, groups = [], [], []
    for line in text.splitlines():
        tokens = line.split()
        if tokens and tokens[0] == "g":
            groups.append(tokens[1])
        elif tokens and tokens[0] == "v":
            vertices.append([float(value) for value in tokens[1:]])
        elif tokens and tokens[0] == "f":
            indices = [int(value) for value in tokens[1:]]
            assert len(indices) == 3 and all(1 <= i <= len(vertices) for i in indices)
            faces.append(indices)
    assert groups == ["b1", "b2", "b3"]
    assert np.allclose(vertices, np.vstack([m.vertices for m in meshes]), atol=.000501, rtol=0)
    offset = 1
    expected_faces = []
    for mesh in meshes:
        expected_faces.extend((mesh.faces + offset).tolist())
        offset += len(mesh.vertices)
    assert faces == expected_faces
    restored = np.asarray(vertices) + np.array([*origin, 0])
    assert restored[:, 0].min() == pytest.approx(E, abs=.001)
    assert restored[:, 1].min() == pytest.approx(N, abs=.001)


@pytest.mark.parametrize("floor,height", [
    (-1, 10), (10, 10), (11, 10), (0, 0), (0, float("inf")), (float("nan"), 10),
])
def test_invalid_heights(floor, height):
    with pytest.raises(ExtrusionError):
        extrude("b1", geometries()[0], floor, height, (E, N))


@pytest.mark.parametrize("geometry", [
    Polygon(), Point(E, N),
    Polygon([(E, N), (E + 10, N + 10), (E, N + 10), (E + 10, N)]),
])
def test_invalid_geometry(geometry):
    with pytest.raises(ExtrusionError):
        extrude("b1", geometry, 0, 10, (E, N))


def test_ids_origins_and_output_validation():
    geometry = geometries()[0]
    origin = shared_origin([geometry])
    mesh = extrude("way:1.part-2_3", geometry, 0, 10, origin)
    assert obj_group_name(mesh.building_id) == "way:1.part-2_3"
    with pytest.raises(ExtrusionError):
        extrude("bad/id", geometry, 0, 10, origin)
    with pytest.raises(ExtrusionError):
        extrude("b1", geometry, 0, 10, (float("nan"), N))
    with pytest.raises(ExtrusionError):
        shared_origin([])
    with pytest.raises(ExtrusionError):
        shared_origin([Polygon()])
    for meshes, target, header in [([mesh, mesh], origin, ()),
                                   ([mesh], (0, 0), ()),
                                   ([mesh], origin, ("bad\nheader",))]:
        with pytest.raises(ExtrusionError):
            write_obj(meshes, target, header)


def test_per_building_and_total_vertex_limits(monkeypatch):
    geometry = geometries()[0]
    origin = shared_origin([geometry])
    mesh = extrude("b1", geometry, 0, 10, origin)
    monkeypatch.setattr(extrusion, "MAX_VERTICES_PER_BUILDING", 7)
    with pytest.raises(ExtrusionError):
        extrude("b1", geometry, 0, 10, origin)
    monkeypatch.setattr(extrusion, "MAX_TOTAL_VERTICES", 7)
    with pytest.raises(ExtrusionError):
        write_obj([mesh], origin)


@pytest.mark.parametrize("faces", [
    np.empty((0, 3), dtype=np.int64), np.array([[0, 1]]),
    np.array([[-1, 1, 2]]), np.array([[0, 1, 8]]), np.array([[0, 0, 1]]),
])
def test_bad_face_arrays(faces):
    with pytest.raises(ExtrusionError):
        check_faces(faces, 8)


@pytest.mark.parametrize("text", [
    "v 0 0 0\n", "g b1\ng b1\n", "g b1\nv 0 0 0\nf 0 1 1\n",
    "g b1\nv 0 0 0\nf 1 2 3\n", "g b1\nv 0 0 0\nf 1 1\n", "g b1\nusemtl x\n",
])
def test_obj_parser_rejects_bad_groups_records_and_indices(text):
    with pytest.raises(ExtrusionError):
        parse_obj_counts(text)
