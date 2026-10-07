"""Offline joins using explicitly synthetic sites and metre-coordinate geometry."""

import pytest
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box

from massing.match import METHOD_POINT, METHOD_POLYGON, Site, match_sites
from massing.simplify import prepare_footprints, to_projected, to_wgs84

E, N = 510000.0, 5450000.0


def site(geometry, sid="s1", reference="test-only://application"):
    return Site(sid, to_wgs84(geometry), reference)


def finding(result, code):
    matches = [item for item in result.findings if item.code == code]
    assert matches, (code, result.findings)
    assert all(item.detail for item in matches)
    return matches


def test_fixture_polygon_point_and_unmatched(footprints_sample, sites_sample):
    batch = prepare_footprints([
        (feature["id"], feature["geometry"]) for feature in footprints_sample["features"]
    ])
    assert not batch.exclusions and batch.reconciled
    sites = [
        Site(f["id"], f["geometry"], f["properties"]["evidence_reference"])
        for f in sites_sample["features"]
    ]
    result = match_sites(sites, {f.building_id: f.geom_utm for f in batch.footprints})
    assert (result.site_count, result.footprint_count) == (3, 5)
    assert {(m.site_id, m.footprint_id, m.method) for m in result.matches} == {
        ("site-b1-b2", "b1", METHOD_POLYGON),
        ("site-b1-b2", "b2", METHOD_POLYGON),
        ("site-near-b3", "b3", METHOD_POINT),
    }
    assert result.unmatched_site_ids == ("site-unmatched",)
    assert result.unmatched_footprint_ids == ("b4", "b5")
    assert not result.conflicted_footprint_ids
    assert finding(result, "no_candidates")[0].subject_id == "site-unmatched"
    for match in result.matches:
        assert match.project_ref.startswith("test-only://")
        if match.method == METHOD_POLYGON:
            assert match.candidate_count == 2 and match.intersection_area_m2 > 0
        else:
            assert match.candidate_count == 1 and 0 < match.distance_m < 25


@pytest.mark.parametrize("distance,matched", [(0, True), (24.99, True), (25, True), (25.01, False)])
def test_point_radius_in_metres_including_boundary(distance, matched):
    candidate = site(Point(E, N))
    point = to_projected(candidate.geometry)
    footprint = box(point.x + distance, point.y - 5, point.x + distance + 10, point.y + 5)
    result = match_sites([candidate], {"b1": footprint})
    assert bool(result.matches) is matched
    if matched:
        assert result.matches[0].distance_m == pytest.approx(distance, abs=.001)
        assert result.matches[0].method == METHOD_POINT
    else:
        finding(result, "no_candidates")
        assert result.unmatched_site_ids == ("s1",)


def test_point_selects_nearest_not_id_order():
    footprints = {"a": box(E + 15, N, E + 20, N + 10),
                  "z": box(E + 5, N, E + 10, N + 10)}
    result = match_sites([site(Point(E, N + 5))], footprints)
    assert len(result.matches) == 1
    assert (result.matches[0].footprint_id, result.matches[0].candidate_count) == ("z", 2)


@pytest.mark.parametrize("kind", ["Point", "Polygon"])
def test_more_than_three_candidates_are_not_guessed(kind):
    footprints = {f"b{i}": box(E + i * 5, N, E + i * 5 + 4, N + 4) for i in range(4)}
    geometry = Point(E - 1, N + 2) if kind == "Point" else box(E - 2, N - 2, E + 25, N + 6)
    result = match_sites([site(geometry)], footprints)
    assert not result.matches
    issue = finding(result, "too_many_candidates")[0]
    assert set(issue.candidate_ids) == set(footprints)
    assert result.unmatched_site_ids == ("s1",)
    assert set(result.unmatched_footprint_ids) == set(footprints)


def test_distance_tie_is_reported_not_resolved_by_id():
    footprints = {"left": box(E - 15, N - 5, E - 5, N + 5),
                  "right": box(E + 5, N - 5, E + 15, N + 5)}
    result = match_sites([site(Point(E, N))], footprints)
    assert not result.matches
    assert set(finding(result, "distance_tie")[0].candidate_ids) == set(footprints)


def test_polygon_no_candidates_and_empty_inputs():
    result = match_sites([site(box(E, N, E + 10, N + 10))], {})
    assert not result.matches and result.unmatched_site_ids == ("s1",)
    finding(result, "no_candidates")
    empty = match_sites([], {"b1": box(E, N, E + 10, N + 10)})
    assert empty.site_count == 0 and empty.unmatched_footprint_ids == ("b1",)


@pytest.mark.parametrize("same_reference", [True, False])
def test_project_conflicts_are_visible(same_reference):
    first = site(Point(E + 5, N + 5), "s1", "test-only://project-a")
    second = site(Point(E + 5, N + 5), "s2",
                  "test-only://project-a" if same_reference else "test-only://project-b")
    result = match_sites([second, first], {"b1": box(E, N, E + 10, N + 10)})
    assert len(result.matches) == 2
    assert result.conflicted_footprint_ids == (() if same_reference else ("b1",))
    if not same_reference:
        assert finding(result, "conflicting_project_matches")[0].candidate_ids == ("s1", "s2")


def test_multipart_site_and_courtyard_distance():
    first, second = box(E, N, E + 10, N + 10), box(E + 40, N, E + 50, N + 10)
    result = match_sites([site(MultiPolygon([first.buffer(1), second.buffer(1)]))],
                         {"b1": first, "b2": second})
    assert {m.footprint_id for m in result.matches} == {"b1", "b2"}
    courtyard = Polygon(box(E, N, E + 20, N + 20).exterior.coords,
                        [box(E + 5, N + 5, E + 15, N + 15).exterior.coords])
    result = match_sites([site(Point(E + 10, N + 10))], {"b3": courtyard})
    assert result.matches[0].distance_m == pytest.approx(5, abs=.001)


@pytest.mark.parametrize("geometry,code", [
    (Point(), "empty_geometry"),
    (LineString([(E, N), (E + 10, N)]), "unsupported_site_geometry"),
])
def test_invalid_site_geometry_is_visible(geometry, code):
    result = match_sites([site(geometry)], {})
    assert not result.matches
    finding(result, code)


def test_duplicate_and_invalid_site_ids():
    result = match_sites([site(Point(E, N)), site(Point(E, N))], {})
    assert not result.matches and len(finding(result, "duplicate_site_id")) == 2
    result = match_sites([site(Point(E, N), "bad/id")], {})
    assert not result.matches
    finding(result, "invalid_id")
