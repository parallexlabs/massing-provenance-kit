"""Offline CSV-v2 binding regressions; modified inputs are explicitly synthetic."""

import csv
import hashlib
import io
import json
from copy import deepcopy
from pathlib import Path

import pytest

from massing.csvio import COLUMNS, parse_csv_text, read_csv
from massing.csvvalidate import parse_json_document, raw_geometry, validate
from massing.simplify import GeometryError

REF = "test-only://not-city-evidence"
ROOT = Path(__file__).resolve().parents[1]


def rectangle(west, south, east, north):
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


def csv_document(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    return stream.getvalue()


@pytest.fixture
def packet():
    rows = [["Synthetic A", "Synthetic location A", " 24 meters ", "1", "", " Raw status A ", REF],
            ["Synthetic B", "Synthetic location B", "10 storeys", "1", "", "Raw status B", REF]]
    features, manifest = [], {}
    for index, (row, west) in enumerate(zip(rows, [-122.85, -122.8495], strict=True), start=1):
        fid = f"b{index}"
        features.append({"type": "Feature", "id": fid,
                         "properties": {"id": fid, "name": row[0], "location": row[1],
                                        "test_only": True, "original_note": {"synthetic": True}},
                         "geometry": {"type": "Polygon", "coordinates": [
                             rectangle(west, 49.19, west + .0003, 49.1902)]}})
        manifest[fid] = {"location_point": [west + .00015, 49.1901], "source_url": REF,
                         "page": index, "unit": "meters" if index == 1 else "storeys",
                         "method": "Synthetic manual evidence transcription", "reviewer": "unreviewed",
                         "uncertainty": "SYNTHETIC TEST ONLY", "footprint_source_url": "test-only://geometry",
                         "footprint_method": "Authored synthetic polygon", "schematic": True,
                         "detailed": False, "test_only": True, "fixture_extra": {"note": "preserve me"}}
    collection = {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
                  "features": features}
    return rows, collection, manifest


def run(packet):
    rows, collection, manifest = packet
    return validate(parse_csv_text(csv_document(rows)), collection, manifest)


def counts(result, statuses, features=2, buildings=None, models=None):
    accepted, unknown, rejected, not_evaluated = statuses
    buildings = accepted + unknown if buildings is None else buildings
    models = accepted if models is None else models
    r = result.reconciliation
    assert (r.csv_rows, r.accepted_rows, r.unknown_rows, r.rejected_rows, r.not_evaluated_rows,
            r.footprint_features, r.output_buildings, r.model_buildings, r.unknown_buildings,
            r.unmodelled_features, r.reconciled) == (
                sum(statuses), accepted, unknown, rejected, not_evaluated, features,
                buildings, models, buildings - models, features - buildings, True)
    assert len(result.rows) == sum(statuses) and len(result.buildings) == buildings
    exported = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    assert exported["ok"] is result.ok and exported["fatal"] is result.fatal
    assert exported["reconciliation"]["output_buildings"] == buildings


def rejects_first(packet, code):
    result = run(packet)
    assert not result.ok and not result.fatal
    counts(result, (1, 0, 1, 0))
    assert [b.id for b in result.buildings] == ["b2"]
    assert code in {f.code for f in result.findings}
    assert all(f.message for f in result.findings)
    return result


def test_known_buildings_original_geometry_properties_and_distinct_methods(packet):
    before = deepcopy(packet)
    result = run(packet)
    assert result.ok and not result.fatal and packet == before
    counts(result, (2, 0, 0, 0))
    for building, expected in zip(result.buildings, [24, 32.5], strict=True):
        fp = building.footprint
        geometry = packet[1]["features"][int(building.id[1:]) - 1]["geometry"]
        digest = hashlib.sha256(json.dumps(geometry, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        assert fp.geometry == geometry and fp.geometry_sha256 == digest and fp.geom_wgs84.is_valid
        assert fp.geom_utm.is_valid and fp.area_m2 > 10 and building.height_m == expected
        assert fp.physical_id == building.id and building.has_model
        pv = building.provenance_record()
        assert pv["height_method"] == building.row.height.method
        assert pv["source_method"] == packet[2][building.id]["method"] != pv["height_method"]
        assert pv["geometry_sha256"] == digest and pv["source_url"] == REF and pv["test_only"] is True
        assert pv["extras"] == {"fixture_extra": {"note": "preserve me"}}
        props = building.geojson_properties(f"provenance/{building.id}.json", f"models/{building.id}.gltf")
        assert all(props[key] == value for key, value in fp.properties.items())
        assert [props[f"csv_{column}"] for column in COLUMNS] == list(building.row.raw)
        assert props["height_m"] == props["extrusion_height_m"] == expected
        assert props["base_height_m"] == 0 and props["schematic"] is True and props["detailed"] is False
        assert props["height_unit"] == building.row.height.unit
        assert props["evidence_method"] == pv["source_method"] and props["evidence_reviewer"] == "unreviewed"
        with pytest.raises(ValueError):
            building.geojson_properties("provenance.json", None)


def test_frozen_real_examples_are_local_inputs_not_new_public_claims():
    directory = ROOT / "examples/csv2massing/surrey"
    collection = json.loads((directory / "footprints.geojson").read_text())
    manifest = json.loads((directory / "provenance-manifest.json").read_text())
    result = validate(read_csv(directory / "buildings.csv"), collection, manifest)
    assert result.ok
    counts(result, (2, 0, 0, 0))
    assert {b.id: b.height_m for b in result.buildings} == {"surrey-2354": 86.5, "surrey-132062": 107.5}
    originals = {f["id"]: f for f in collection["features"]}
    for building in result.buildings:
        assert building.footprint.geometry == originals[building.id]["geometry"]
        record = building.provenance_record()
        assert record["source_url"] == manifest[building.id]["source_url"]
        assert record["page"] == manifest[building.id]["page"] and record["input_unit"] == "storeys"
        assert record["reviewer"].startswith("unreviewed") and record["test_only"] is False
        assert record["extras"]["pdf_sha256"] == manifest[building.id]["pdf_sha256"]


def test_unknown_retained_as_null_no_model_and_failure_result(packet):
    packet[0][0][2] = " UNKNOWN "
    packet[2]["b1"]["unit"] = "unknown"
    result = run(packet)
    assert not result.ok and not result.fatal
    counts(result, (1, 1, 0, 0))
    building = result.buildings[0]
    assert building.height_m is None and not building.has_model
    props = building.geojson_properties("provenance/b1.json", None)
    assert props["height_m"] is props["extrusion_height_m"] is props["model_file"] is None
    assert building.provenance_record()["resolved_height_m"] is None
    assert any(f.code == "missing_height" and f.row_number == 2 for f in result.findings)
    with pytest.raises(ValueError):
        building.geojson_properties("provenance/b1.json", "models/b1.gltf")


def test_group_count_area_and_uniform_height_are_not_allocated_or_duplicated(packet):
    rows, collection, manifest = packet
    group = [*rows[0]]
    group[3:5] = ["2", "1200.5"]
    for feature in collection["features"]:
        feature["properties"].update(name=group[0], location=group[1])
    manifest["b2"]["unit"] = "m"
    result = run(([group], collection, manifest))
    assert result.ok
    counts(result, (1, 0, 0, 0), buildings=2, models=2)
    assert {b.footprint.physical_id for b in result.buildings} == {"b1", "b2"}
    for building in result.buildings:
        record = building.provenance_record()
        assert building.height_m == 24 and record["building_count"] == 2
        assert record["group_floor_area_m2"] == 1200.5 and "all 2" in record["uniform_height_assumption"]
        assert building.geojson_properties("p.json", "m.gltf")["group_floor_area_m2"] == 1200.5


@pytest.mark.parametrize("crs,code", [
    (None, "missing_crs"), ({"type": "name", "properties": {"name": "EPSG:26910"}}, "wrong_crs"),
    ({"type": "name", "properties": {"name": []}}, "wrong_crs"),
    ({"type": "name", "properties": {"name": {}}}, "wrong_crs"), ([], "wrong_crs"),
])
def test_fatal_crs_reports_raw_feature_count_without_typeerror(packet, crs, code):
    packet[1]["crs"] = crs
    result = run(packet)
    assert result.fatal and not result.ok and code in {f.code for f in result.findings}
    counts(result, (0, 0, 0, 2))


@pytest.mark.parametrize("target,value,code,features", [
    ("collection", None, "malformed_footprints", 0),
    ("collection", {"type": "Polygon"}, "malformed_footprints", 0),
    ("features", None, "malformed_footprints", 0),
    ("manifest", [], "malformed_provenance", 2),
])
def test_malformed_top_level_inputs_are_fatal(packet, target, value, code, features):
    rows, collection, manifest = packet
    if target == "collection":
        collection = value
    elif target == "features":
        collection["features"] = value
    else:
        manifest = value
    result = run((rows, collection, manifest))
    assert result.fatal and code in {f.code for f in result.findings}
    counts(result, (0, 0, 0, 2), features=features)


@pytest.mark.parametrize("target,key,value,code", [
    ("feature", "type", "Polygon", "malformed_footprints"),
    ("feature", "id", 1, "invalid_id"), ("feature", "id", "../unsafe", "invalid_id"),
    ("feature", "properties", None, "invalid_id"), ("properties", "id", "different", "invalid_id"),
    ("properties", "name", "", "missing_binding"), ("properties", "location", [], "missing_binding"),
    ("properties", "physical_building_id", "bad/id", "invalid_id"),
])
def test_malformed_feature_identity_or_binding_rejects_without_invention(packet, target, key, value, code):
    feature = packet[1]["features"][0]
    container = feature if target == "feature" else feature["properties"]
    container[key] = value
    rejects_first(packet, code)


@pytest.mark.parametrize("key", ["height_m", "model_file", "csv_height", "schematic", "evidence_page", "row_number"])
def test_reserved_output_collisions_rejected(packet, key):
    packet[1]["features"][0]["properties"][key] = "collision"
    rejects_first(packet, "reserved_property")


@pytest.mark.parametrize("kind", ["feature_id", "physical_id", "overlap"])
def test_duplicate_components_and_overlapping_podium_tower_all_rejected(packet, kind):
    first, second = packet[1]["features"]
    if kind == "feature_id":
        second["id"] = second["properties"]["id"] = first["id"]
    elif kind == "physical_id":
        for feature in (first, second):
            feature["properties"]["physical_building_id"] = "one-physical-building"
    else:
        second["geometry"] = deepcopy(first["geometry"])
        packet[2]["b2"]["location_point"] = packet[2]["b1"]["location_point"][:]
    result = run(packet)
    assert not result.ok and not result.fatal
    counts(result, (0, 0, 2, 0))
    code = "overlapping_footprints" if kind == "overlap" else "duplicate_id"
    if kind == "feature_id":
        assert sum(f.code == "duplicate_id" for f in result.findings) >= 2
    else:
        assert {f.feature_id for f in result.findings if f.code == code} == {"b1", "b2"}


def test_touching_footprints_and_boundary_location_are_valid(packet):
    edge = packet[1]["features"][0]["geometry"]["coordinates"][0][1][0]
    packet[1]["features"][1]["geometry"]["coordinates"] = [rectangle(edge, 49.19, edge + .0003, 49.1902)]
    packet[2]["b2"]["location_point"] = [edge, 49.1901]
    result = run(packet)
    assert result.ok
    counts(result, (2, 0, 0, 0))


def test_count_mismatch_duplicate_csv_binding_and_unbound_inputs(packet):
    mismatch = deepcopy(packet)
    mismatch[0][0][3] = "2"
    rejects_first(mismatch, "building_count_mismatch")
    duplicated = deepcopy(packet)
    duplicated[0].append(duplicated[0][0][:])
    result = run(duplicated)
    counts(result, (1, 0, 2, 0))
    assert "conflicting_binding" in {f.code for f in result.findings}
    missing = deepcopy(packet)
    missing[0][0][1] = "Unmatched synthetic location"
    result = rejects_first(missing, "missing_footprint")
    assert "unbound_footprint" in {f.code for f in result.findings}
    extra = deepcopy(packet)
    extra[2]["orphan"] = deepcopy(extra[2]["b1"])
    result = run(extra)
    assert not result.ok and "unbound_provenance" in {f.code for f in result.findings}
    counts(result, (2, 0, 0, 0))


@pytest.mark.parametrize("key", ["location_point", "source_url", "page", "unit", "method", "reviewer", "uncertainty",
                                "footprint_source_url", "footprint_method", "schematic", "detailed", "test_only"])
def test_missing_required_provenance_fields(packet, key):
    del packet[2]["b1"][key]
    rejects_first(packet, "invalid_provenance")


@pytest.mark.parametrize("key,value,code", [
    ("page", True, "invalid_provenance"), ("page", 0, "invalid_provenance"),
    ("reviewer", "unknown", "invalid_provenance"), ("reviewer", " ", "invalid_provenance"),
    ("schematic", False, "invalid_provenance"), ("detailed", True, "invalid_provenance"),
    ("test_only", "true", "invalid_provenance"), ("test_only", False, "test_only_required"),
    ("source_url", "https://example.org/other.pdf", "provenance_mismatch"),
    ("unit", "storeys", "provenance_mismatch"), ("unit", "ft", "malformed_unit"),
    ("footprint_source_url", "https://[broken", "invalid_provenance"),
    ("footprint_source_url", "https://:443/report.pdf", "invalid_provenance"),
    ("footprint_source_url", "https://example.org:bad/report.pdf", "invalid_provenance"),
    ("location_point", [True, 49.19], "invalid_provenance"),
    ("location_point", [-122.84, 49.2], "off_footprint"),
])
def test_bad_provenance_has_plain_findings(packet, key, value, code):
    packet[2]["b1"][key] = value
    rejects_first(packet, code)


def test_missing_manifest_record_and_valid_unit_crs_aliases(packet):
    missing = deepcopy(packet)
    del missing[2]["b1"]
    rejects_first(missing, "missing_provenance")
    packet[1]["crs"]["properties"]["name"] = "urn:ogc:def:crs:EPSG::4326"
    packet[2]["b1"]["unit"] = " METRES "
    assert run(packet).ok


@pytest.mark.parametrize("kind", ["holes", "multipart"])
def test_valid_holes_and_multipart_preserved_with_hole_point_rejected(packet, kind):
    geometry = packet[1]["features"][0]["geometry"]
    outer = geometry["coordinates"][0]
    hole = rectangle(-122.8499, 49.19005, -122.8498, 49.19015)
    if kind == "holes":
        geometry["coordinates"].append(hole)
    else:
        geometry.update(type="MultiPolygon", coordinates=[[outer], [rectangle(-122.851, 49.19, -122.8507, 49.1902)]])
    packet[2]["b1"]["location_point"] = [-122.84998, 49.1901]
    original = deepcopy(geometry)
    result = run(packet)
    assert result.ok and result.buildings[0].footprint.geometry == original
    counts(result, (2, 0, 0, 0))
    if kind == "holes":
        packet[2]["b1"]["location_point"] = [-122.84985, 49.1901]
        rejects_first(packet, "off_footprint")


@pytest.mark.parametrize("kind", ["open", "short", "self_intersection", "empty", "z", "nonfinite",
                                 "out_of_range", "outside_area", "type", "multipart", "missing"])
def test_invalid_raw_geometry_not_repaired_or_autoclosed(packet, kind):
    geometry = packet[1]["features"][0]["geometry"]
    ring = geometry["coordinates"][0]
    if kind == "open":
        ring.pop()
    elif kind == "short":
        geometry["coordinates"] = [ring[:3]]
    elif kind == "self_intersection":
        geometry["coordinates"] = [[ring[0], ring[2], ring[1], ring[3], ring[0]]]
    elif kind == "empty":
        geometry["coordinates"] = []
    elif kind == "z":
        ring[1].append(10)
    elif kind == "nonfinite":
        ring[1][0] = float("nan")
    elif kind == "out_of_range":
        ring[1][0] = 181
    elif kind == "outside_area":
        geometry["coordinates"] = [rectangle(0, 49.19, .001, 49.191)]
    elif kind == "type":
        geometry["type"] = "Point"
    elif kind == "multipart":
        geometry.update(type="MultiPolygon", coordinates=[[ring], [deepcopy(ring)]])
    else:
        geometry = packet[1]["features"][0]["geometry"] = None
    if kind != "outside_area":
        with pytest.raises(GeometryError):
            raw_geometry(geometry)
    rejects_first(packet, "invalid_polygon")


@pytest.mark.parametrize("data,code", [
    (b'{"a":1,"a":2}', "duplicate_id"), (b'{"x":{"a":1,"a":2}}', "duplicate_id"),
    (b"\xff", "malformed_footprints"), (b'{"x":NaN}', "malformed_footprints"),
    (b'{"x":Infinity}', "malformed_footprints"), (b"{broken", "malformed_footprints"),
    (("[" * 2000 + "0" + "]" * 2000).encode(), "malformed_footprints"),
])
def test_strict_json_reports_duplicates_nonfinite_utf8_and_recursion(data, code):
    document, findings = parse_json_document(data, "footprints")
    assert document is None and len(findings) == 1 and findings[0].code == code
    assert findings[0].message and "Traceback" not in findings[0].message


def test_json_success_and_csv_file_error_vs_partial_row_rejection(packet):
    collection, findings = parse_json_document(json.dumps(packet[1]).encode(), "footprints")
    assert collection == packet[1] and not findings
    bad_file = parse_csv_text(csv_document(packet[0]) + '"unterminated')
    result = validate(bad_file, collection, packet[2])
    assert result.fatal and not result.ok
    counts(result, (0, 0, 0, 2))
    bad_row = deepcopy(packet)
    bad_row[0][0][2] = "24"
    result = rejects_first(bad_row, "unitless_height")
    assert result.rows[0].row_number == 2 and result.rows[0].status == "rejected"
