"""Offline synthetic geometry, plus the supplied real public 18570 regression."""

import json
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Point, Polygon, box, shape
from shapely.ops import transform

import massing.simplify as simplify
from massing.simplify import (
    GeometryError,
    footprint_width_m,
    prepare_footprint,
    prepare_footprints,
    to_projected,
    to_wgs84,
)

E, N = 510000.0, 5450000.0


def prepared(geometry, bid="synthetic"):
    outcome = prepare_footprint(bid, to_wgs84(geometry))
    assert outcome.exclusion is None, outcome.exclusion
    assert outcome.footprint is not None
    return outcome.footprint


def test_fixture_validity_topology_and_area(footprints_sample):
    batch = prepare_footprints([
        (f["id"], f["geometry"]) for f in footprints_sample["features"]
    ])
    assert batch.input_count == 5 and batch.reconciled and not batch.exclusions
    assert [f.building_id for f in batch.footprints] == ["b1", "b2", "b3", "b4", "b5"]
    for footprint in batch.footprints:
        assert footprint.geom_utm.is_valid and footprint.geom_wgs84.is_valid
        assert abs(footprint.area_m2 / footprint.input_area_m2 - 1) < .01
        assert footprint.area_m2 == pytest.approx(footprint.geom_utm.area)
        assert footprint.width_m > 0 and footprint.output_vertices <= footprint.input_vertices
    by_id = {f.building_id: f for f in batch.footprints}
    assert (by_id["b3"].part_count, by_id["b3"].hole_count) == (1, 1)
    assert (by_id["b4"].part_count, by_id["b4"].hole_count) == (2, 0)


def test_projection_matches_independent_pyproj_and_round_trip():
    geometry = box(-122.85, 49.19, -122.8497, 49.1902)
    reference = transform(Transformer.from_crs(4326, 26910, always_xy=True).transform, geometry)
    projected = to_projected(geometry)
    assert projected.hausdorff_distance(reference) < 1e-6
    assert 400 < projected.area < 600
    assert 20 < footprint_width_m(projected) < 25
    assert to_wgs84(projected).hausdorff_distance(geometry) < 1e-8


def test_small_redundant_vertex_simplifies_in_metres():
    polygon = Polygon([(E, N), (E + 10, N + .05), (E + 20, N),
                       (E + 20, N + 20), (E, N + 20)])
    footprint = prepared(polygon)
    assert footprint.output_vertices < footprint.input_vertices
    assert abs(footprint.area_m2 / footprint.input_area_m2 - 1) < .01
    assert any(change.code == "simplified" for change in footprint.changes)


def test_public_18570_retains_geometry_when_simplification_exceeds_one_percent():
    path = Path(__file__).resolve().parents[1] / "examples/surrey/geometry_regression.geojson"
    data = json.loads(path.read_text(encoding="utf-8"))
    feature = next(f for f in data["features"] if str(f["id"]) == "18570")
    original = to_projected(shape(feature["geometry"]))
    candidate = original.simplify(.3, preserve_topology=True)
    assert original.is_valid and abs(candidate.area / original.area - 1) > .01
    outcome = prepare_footprint("18570", feature["geometry"])
    assert outcome.exclusion is None and outcome.footprint is not None
    footprint = outcome.footprint
    assert abs(footprint.area_m2 / original.area - 1) < .01
    assert footprint.geom_utm.equals(original)
    assert footprint.geom_wgs84.is_valid
    assert outcome.changes and all(change.detail for change in outcome.changes)


@pytest.mark.parametrize("geometry,code", [
    (None, "malformed_geometry"),
    ({}, "malformed_geometry"),
    (Point(-122.85, 49.19), "unsupported_geometry"),
    (Polygon(), "empty_geometry"),
    (box(181, 49, 182, 50), "coordinates_out_of_range"),
    (box(0, 49, .01, 49.01), "outside_crs_area_of_use"),
])
def test_invalid_geometry_excluded(geometry, code):
    outcome = prepare_footprint("synthetic", geometry)
    assert outcome.footprint is None and outcome.exclusion.code == code
    assert outcome.exclusion.detail


def test_nonfinite_coordinates_and_invalid_id():
    geometry = {"type": "Polygon", "coordinates": [[
        [-122.85, 49.19], [float("inf"), 49.19],
        [-122.849, 49.191], [-122.85, 49.19],
    ]]}
    outcome = prepare_footprint("synthetic", geometry)
    assert outcome.footprint is None and outcome.exclusion.code == "nonfinite_coordinates"
    outcome = prepare_footprint("bad/id", box(-122.85, 49.19, -122.849, 49.191))
    assert outcome.footprint is None and outcome.exclusion.code == "invalid_id"


def test_repair_reports_invalid_input_and_dropped_nonpolygonal_parts():
    polygon = Polygon([(E, N), (E + 20, N), (E + 20, N + 20),
                       (E + 10, N + 20), (E + 10, N + 25),
                       (E + 10, N + 20), (E, N + 20)])
    assert not polygon.is_valid
    footprint = prepared(polygon)
    assert footprint.geom_utm.is_valid and footprint.geom_wgs84.is_valid
    assert footprint.area_m2 == pytest.approx(400, abs=.01)
    assert any(change.code == "repaired" and change.detail for change in footprint.changes)


def test_sliver_exclusion_and_multipart_sliver_reporting():
    small, large = box(E, N, E + 3, N + 3), box(E + 20, N, E + 31, N + 10)
    outcome = prepare_footprint("sliver", to_wgs84(small))
    assert outcome.footprint is None and outcome.exclusion.code == "sliver"
    footprint = prepared(MultiPolygon([small, large]))
    assert footprint.part_count == 1 and footprint.area_m2 == pytest.approx(110, abs=.01)
    assert any(change.code == "sliver_part_dropped" for change in footprint.changes)


def test_duplicate_ids_all_excluded_and_totals_reconcile():
    geometry = to_wgs84(box(E, N, E + 10, N + 10))
    batch = prepare_footprints([("b2", geometry), ("b1", geometry),
                                ("b1", None), ("bad/id", geometry)])
    assert batch.input_count == 4 and batch.reconciled
    assert [f.building_id for f in batch.footprints] == ["b2"]
    assert [e.code for e in batch.exclusions].count("duplicate_id") == 2
    assert any(e.code == "invalid_id" for e in batch.exclusions)


def test_z_is_explicitly_dropped_and_vertex_limit_reported(monkeypatch):
    geometry = Polygon([(-122.85, 49.19, 9), (-122.8497, 49.19, 9),
                        (-122.8497, 49.1902, 9), (-122.85, 49.1902, 9)])
    outcome = prepare_footprint("synthetic", geometry)
    assert outcome.footprint is not None and not outcome.footprint.geom_wgs84.has_z
    assert any(change.code == "z_dropped" for change in outcome.changes)
    monkeypatch.setattr(simplify, "MAX_INPUT_VERTICES", 4)
    outcome = prepare_footprint("synthetic", geometry)
    assert outcome.footprint is None and outcome.exclusion.code == "too_many_vertices"


def test_invalid_inverse_geometry_is_excluded(monkeypatch):
    geometry = to_wgs84(box(E, N, E + 20, N + 20))
    invalid = Polygon([(-122.85, 49.19), (-122.849, 49.191),
                       (-122.85, 49.191), (-122.849, 49.19)])
    assert not invalid.is_valid
    monkeypatch.setattr(simplify, "to_wgs84", lambda _: invalid)
    outcome = prepare_footprint("synthetic", geometry)
    assert outcome.footprint is None
    assert outcome.exclusion is not None and outcome.exclusion.detail


def test_nonfinite_projection_is_an_explicit_error():
    with pytest.raises(GeometryError):
        to_projected(Point(float("inf"), 49.19))
    assert np.isfinite(to_projected(Point(-122.85, 49.19)).coords).all()
