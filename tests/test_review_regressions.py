"""Offline review regressions for strict JSON, row binding and artifact safety."""

import csv
import json
from copy import deepcopy

import pytest
from typer.testing import CliRunner

from massing.cli import OutputDirError, app, write_artefacts
from massing.csvio import COLUMNS, read_csv
from massing.csvvalidate import parse_json_document, validate

BAD_EXTRAS = [
    pytest.param('{"nested":[{"value":1e999}]}', id="positive-exponent-overflow"),
    pytest.param('{"nested":[{"value":-1e999}]}', id="negative-exponent-overflow"),
    pytest.param('{"nested":[{"value":1e+999}]}', id="explicit-positive-exponent"),
    pytest.param(r'{"nested":{"\ud800":"value"}}', id="high-surrogate-key"),
    pytest.param(r'{"nested":{"\udfff":"value"}}', id="low-surrogate-key"),
    pytest.param(r'{"nested":[{"value":"\ud800"}]}', id="high-surrogate-value"),
    pytest.param(r'{"nested":[{"value":"\udfff"}]}', id="low-surrogate-value"),
    pytest.param(r'{"nested":[{"value":"prefix\ud800suffix"}]}', id="embedded-surrogate"),
]


@pytest.fixture
def packet():
    rows, features, manifest = [], [], {}
    for index, west in enumerate([-122.85, -122.8494, -122.8488], start=1):
        fid = f"b{index}"
        name, location = f"SYNTHETIC Building {index}", f"SYNTHETIC Location {index}"
        source = f"test-only://review/building-{index}.pdf"
        rows.append([name, location, "24 m", "1", "500", "SYNTHETIC status", source])
        features.append({
            "type": "Feature",
            "id": fid,
            "properties": {
                "id": fid,
                "physical_building_id": f"physical-{fid}",
                "name": name,
                "location": location,
                "fixture_note": "SYNTHETIC TEST ONLY; not a real building.",
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [west, 49.19], [west + .0003, 49.19],
                    [west + .0003, 49.1902], [west, 49.1902], [west, 49.19],
                ]],
            },
        })
        manifest[fid] = {
            "location_point": [west + .00015, 49.1901],
            "source_url": source,
            "page": 1,
            "unit": "m",
            "method": "Synthetic explicit-height fixture",
            "reviewer": "unreviewed synthetic fixture",
            "uncertainty": "Test-only fictional building; not City evidence.",
            "footprint_source_url": "test-only://review/geometry",
            "footprint_method": "Explicitly authored synthetic WGS84 coordinates",
            "schematic": True,
            "detailed": False,
            "test_only": True,
        }
    collection = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": features,
    }
    return rows, collection, manifest


def write_inputs(tmp_path, packet, corrupt=None):
    rows, collection, manifest = packet
    directory = tmp_path / "inputs"
    directory.mkdir()
    paths = {
        "csv": directory / "buildings.csv",
        "footprints": directory / "footprints.geojson",
        "provenance": directory / "provenance.json",
    }
    with paths["csv"].open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    documents = {"footprints": deepcopy(collection), "provenance": deepcopy(manifest)}
    if corrupt is not None:
        what, fragment = corrupt
        marker = "SYNTHETIC_REVIEW_JSON_FRAGMENT"
        if what == "footprints":
            documents[what]["features"][0]["properties"]["review_extra"] = marker
        else:
            documents[what][next(iter(documents[what]))]["review_extra"] = marker
    for what, document in documents.items():
        text = json.dumps(document, ensure_ascii=True, allow_nan=False)
        if corrupt is not None and what == corrupt[0]:
            assert text.count(json.dumps(marker)) == 1
            text = text.replace(json.dumps(marker), corrupt[1], 1)
        paths[what].write_bytes(text.encode("utf-8"))
    return paths


def validated(paths):
    collection, fp_findings = parse_json_document(paths["footprints"].read_bytes(), "footprints")
    manifest, pv_findings = parse_json_document(paths["provenance"].read_bytes(), "provenance")
    assert not fp_findings and not pv_findings
    return validate(read_csv(paths["csv"]), collection, manifest)


def invoke(paths, out):
    return CliRunner().invoke(app, [
        str(paths["csv"]), "--footprints", str(paths["footprints"]),
        "--provenance", str(paths["provenance"]), "--out", str(out),
    ])


def assert_validator(result, statuses, ids, features=3):
    assert [row.status for row in result.rows] == statuses
    assert {building.id for building in result.buildings} == set(ids)
    r = result.reconciliation
    assert (r.csv_rows, r.accepted_rows, r.unknown_rows, r.rejected_rows,
            r.not_evaluated_rows, r.footprint_features, r.output_buildings,
            r.model_buildings, r.unknown_buildings, r.unmodelled_features) == (
                len(statuses), statuses.count("accepted"), 0, statuses.count("rejected"),
                0, features, len(ids), len(ids), 0, features - len(ids))
    assert r.reconciled and not result.fatal
    assert result.ok is all(status == "accepted" for status in statuses)
    json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False).encode("utf-8")


def assert_outputs(out, statuses, ids, features=3, fatal=False):
    report = json.loads((out / "validation.json").read_bytes())
    collection = json.loads((out / "massing.geojson").read_bytes())
    assert (out / "validation.md").read_text(encoding="utf-8")
    assert (out / "roundtrip.csv").is_file()
    assert report["fatal"] is fatal
    assert report["ok"] is (not fatal and all(status == "accepted" for status in statuses))
    assert [row["status"] for row in report["rows"]] == statuses
    r = report["reconciliation"]
    assert (r["csv_rows"], r["accepted_rows"], r["unknown_height_rows"],
            r["rejected_rows"], r["not_evaluated_rows"], r["footprint_features"],
            r["output_buildings"], r["models_written"], r["unknown_height_buildings"],
            r["provenance_files_written"]) == (
                len(statuses), statuses.count("accepted"), 0,
                statuses.count("rejected"), statuses.count("not_evaluated"),
                features, len(ids), len(ids), 0, len(ids))
    assert r["reconciled"]
    assert report["validation_reconciliation"]["unmodelled_features"] == features - len(ids)
    assert {feature["id"] for feature in collection["features"]} == set(ids)
    assert {path.stem for path in (out / "models").glob("*.gltf")} == set(ids)
    assert {path.stem for path in (out / "provenance").glob("*.json")} == set(ids)
    for feature in collection["features"]:
        props = feature["properties"]
        assert props["evidence_test_only"] is True
        assert props["height_m"] == 24 and props["model_file"] is not None
        assert (out / props["model_file"]).is_file()
        record = json.loads((out / props["provenance_file"]).read_bytes())
        assert record["test_only"] is True and record["resolved_height_m"] == 24
    json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return report, collection


def assert_plain_failure(result):
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "Traceback" not in result.output and "internal error" not in result.output


@pytest.mark.parametrize("what", ["footprints", "provenance"])
@pytest.mark.parametrize("fragment", BAD_EXTRAS)
def test_strict_json_rejects_nested_overflow_and_lone_surrogates(what, fragment):
    document, findings = parse_json_document(fragment.encode("ascii"), what)
    assert document is None and len(findings) == 1
    finding, = findings
    assert finding.code == f"malformed_{what}" and finding.severity == "error"
    assert finding.message and "Traceback" not in finding.message


@pytest.mark.parametrize("what", ["footprints", "provenance"])
@pytest.mark.parametrize("fragment", BAD_EXTRAS)
def test_cli_bad_json_extras_are_fatal_plain_errors_without_models(
    packet, tmp_path, what, fragment,
):
    paths = write_inputs(tmp_path, packet, corrupt=(what, fragment))
    out = tmp_path / "output"
    result = invoke(paths, out)
    assert_plain_failure(result)
    feature_count = 0 if what == "footprints" else 3
    report, _ = assert_outputs(
        out, ["not_evaluated"] * 3, (), features=feature_count, fatal=True,
    )
    assert any(f["code"] == f"malformed_{what}" and f["message"] for f in report["findings"])
    assert f"malformed_{what}" in result.output


def test_valid_unicode_surrogate_pairs_and_finite_exponents_are_accepted():
    body = (
        br'{"\ud83d\ude00":{"text":"caf\u00e9 \u6f22 \ud83d\ude00",'
        br'"finite":[1e2,-2.5e-3,1e308,5e-324,1e-999]}}'
    )
    document, findings = parse_json_document(body, "provenance")
    assert not findings
    values = document["\U0001f600"]
    assert values["text"] == "caf\u00e9 \u6f22 \U0001f600"
    assert values["finite"] == [100, -.0025, 1e308, 5e-324, 0]
    encoded = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    again, findings = parse_json_document(encoded, "footprints")
    assert again == document and not findings


def test_valid_unicode_and_finite_extras_survive_validator_and_cli(packet, tmp_path):
    body = (
        br'{"\ud83d\ude00":{"text":"caf\u00e9 \u6f22",'
        br'"finite":[1e2,-2.5e-3,1e308,5e-324]}}'
    )
    extra = json.loads(body)
    packet[1]["features"][0]["properties"]["review_extra"] = deepcopy(extra)
    packet[2]["b1"]["review_extra"] = deepcopy(extra)
    paths = write_inputs(tmp_path, packet)
    result = validated(paths)
    assert_validator(result, ["accepted"] * 3, ("b1", "b2", "b3"))
    building = next(building for building in result.buildings if building.id == "b1")
    assert building.provenance.extras["review_extra"] == extra
    out = tmp_path / "output"
    assert invoke(paths, out).exit_code == 0
    _, collection = assert_outputs(out, ["accepted"] * 3, ("b1", "b2", "b3"))
    first = next(feature for feature in collection["features"] if feature["id"] == "b1")
    assert first["properties"]["review_extra"] == extra
    record = json.loads((out / first["properties"]["provenance_file"]).read_bytes())
    assert record["extras"]["review_extra"] == extra
    assert "\u6f22" in (out / "provenance/b1.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("bad_id", ["missing", "numeric", "traversal"])
def test_invalid_id_sibling_rejects_whole_row_and_keeps_unrelated_building(
    packet, tmp_path, bad_id,
):
    rows, collection, manifest = packet
    sibling = collection["features"][1]
    sibling["properties"].update(name=rows[0][0], location=rows[0][1])
    if bad_id == "missing":
        del sibling["id"]
    elif bad_id == "numeric":
        sibling["id"] = sibling["properties"]["id"] = 7
    else:
        sibling["id"] = sibling["properties"]["id"] = "../unsafe"
    del rows[1]
    del manifest["b2"]
    paths = write_inputs(tmp_path, packet)
    result = validated(paths)
    assert_validator(result, ["rejected", "accepted"], ("b3",))
    assert any(f.code == "invalid_id" for f in result.findings)
    assert any(f.code == "invalid_footprint" and f.row_number == 2 for f in result.findings)
    out = tmp_path / "output"
    cli_result = invoke(paths, out)
    assert_plain_failure(cli_result)
    report, _ = assert_outputs(out, ["rejected", "accepted"], ("b3",))
    assert any(f["code"] == "invalid_footprint" and f["row_number"] == 2 for f in report["findings"])
    assert not (out / "models/b1.gltf").exists()
    assert not (out / "provenance/b1.json").exists()


@pytest.mark.parametrize("second_height", ["24 m", "unknown"])
def test_casefold_feature_id_collisions_reject_known_and_unknown_rows(
    packet, tmp_path, second_height,
):
    rows, collection, manifest = packet
    for feature, old_id, new_id in zip(
        collection["features"][:2], ("b1", "b2"), ("A", "a"), strict=True,
    ):
        feature["id"] = feature["properties"]["id"] = new_id
        manifest[new_id] = manifest.pop(old_id)
    rows[1][2] = second_height
    if second_height == "unknown":
        manifest["a"]["unit"] = "unknown"
    assert collection["features"][0]["properties"]["physical_building_id"] != (
        collection["features"][1]["properties"]["physical_building_id"]
    )
    paths = write_inputs(tmp_path, packet)
    result = validated(paths)
    assert_validator(result, ["rejected", "rejected", "accepted"], ("b3",))
    assert {f.feature_id for f in result.findings if f.code == "duplicate_id"} == {"A", "a"}
    assert not any(f.code == "missing_height" for f in result.findings)
    out = tmp_path / "output"
    cli_result = invoke(paths, out)
    assert_plain_failure(cli_result)
    report, _ = assert_outputs(out, ["rejected", "rejected", "accepted"], ("b3",))
    assert {f["feature_id"] for f in report["findings"] if f["code"] == "duplicate_id"} == {"A", "a"}
    assert "duplicate_id" in cli_result.output


def test_physical_ids_keep_exact_case_semantics_with_distinct_feature_ids(packet, tmp_path):
    packet[1]["features"][0]["properties"]["physical_building_id"] = "A"
    packet[1]["features"][1]["properties"]["physical_building_id"] = "a"
    paths = write_inputs(tmp_path, packet)
    result = validated(paths)
    assert_validator(result, ["accepted"] * 3, ("b1", "b2", "b3"))
    assert {building.footprint.physical_id for building in result.buildings} == {
        "A", "a", "physical-b3",
    }
    out = tmp_path / "output"
    assert invoke(paths, out).exit_code == 0
    _, collection = assert_outputs(out, ["accepted"] * 3, ("b1", "b2", "b3"))
    for feature in collection["features"]:
        model = json.loads((out / feature["properties"]["model_file"]).read_bytes())
        assert model["extras"]["physical_building_id"] == feature["properties"]["physical_building_id"]


def tree_snapshot(root):
    paths = [root, *root.rglob("*")]
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in paths
    }


@pytest.mark.parametrize("existing_empty", [False, True])
@pytest.mark.parametrize("pair", [
    ("models/A.gltf", "models/a.gltf"),
    ("provenance/A.json", "provenance/a.json"),
    ("Models/A.gltf", "models/a.GLTF"),
])
def test_writer_casefold_collision_refused_before_any_mutation(tmp_path, existing_empty, pair):
    out = tmp_path / "output"
    if existing_empty:
        out.mkdir()
    (tmp_path / "sentinel").write_bytes(b"unchanged")
    before = tree_snapshot(tmp_path)
    artifacts = {"aaa.txt": b"must not be written", pair[0]: b"first", pair[1]: b"second"}
    with pytest.raises(OutputDirError) as caught:
        write_artefacts(out, artifacts)
    assert str(caught.value) and "Traceback" not in str(caught.value)
    assert tree_snapshot(tmp_path) == before
    assert out.exists() is existing_empty
    if existing_empty:
        assert not list(out.iterdir())


@pytest.mark.parametrize("existing_empty", [False, True])
def test_writer_accepts_distinct_artifact_paths_and_preserves_exact_bytes(tmp_path, existing_empty):
    out = tmp_path / "output"
    if existing_empty:
        out.mkdir()
    artifacts = {
        "models/A.gltf": b"SYNTHETIC model bytes A",
        "models/B.gltf": b"SYNTHETIC model bytes B",
        "provenance/A.json": b'{"test_only":true}',
        "validation.json": b'{"synthetic":true}',
    }
    write_artefacts(out, artifacts)
    written = {
        path.relative_to(out).as_posix(): path.read_bytes()
        for path in out.rglob("*") if path.is_file()
    }
    assert written == artifacts
