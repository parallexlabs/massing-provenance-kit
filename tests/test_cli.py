"""Offline acceptance regressions for the canonical csv2massing CLI."""

import csv
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import massing.cli as cli
from massing.gltf import GltfError

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/csv2massing/synthetic"


@pytest.fixture
def inputs(tmp_path):
    directory = tmp_path / "inputs"
    directory.mkdir()
    files = {}
    for key, name in [("csv", "buildings.csv"), ("bad", "bad.csv"),
                      ("footprints", "footprints.geojson"), ("provenance", "provenance-manifest.json")]:
        files[key] = directory / name
        shutil.copyfile(EXAMPLE / name, files[key])
    return files


def invoke(inputs, out, csv_path=None):
    return CliRunner().invoke(cli.app, [str(csv_path or inputs["csv"]),
        "--footprints", str(inputs["footprints"]), "--provenance", str(inputs["provenance"]), "--out", str(out)])


def read_cells(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.reader(stream))


def write_cells(path, cells):
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\r\n").writerows(cells)


def write_json(path, value):
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


def artifacts(out):
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}


def outputs(out, accepted, unknown, rejected, not_evaluated=0, rows=10, buildings=None):
    report = json.loads((out / "validation.json").read_text())
    collection = json.loads((out / "massing.geojson").read_text())
    count = accepted + unknown if buildings is None else buildings
    r = report["reconciliation"]
    assert (r["csv_rows"], r["accepted_rows"], r["unknown_height_rows"], r["rejected_rows"],
            r["not_evaluated_rows"], r["output_buildings"], r["models_written"],
            r["unknown_height_buildings"], r["provenance_files_written"], r["reconciled"]) == (
                rows, accepted, unknown, rejected, not_evaluated, count, count - unknown, unknown, count, True)
    assert len(collection["features"]) == count
    assert len(list((out / "models").glob("*.gltf"))) == count - unknown
    assert len(list((out / "provenance").glob("*.json"))) == count
    assert (out / "validation.md").is_file() and (out / "roundtrip.csv").is_file()
    return report, collection["features"]


def test_canonical_help_good_ten_rows_provenance_and_determinism(inputs, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    help_result = CliRunner().invoke(cli.app, ["--help"])
    assert help_result.exit_code == 0 and "--footprints" in help_result.output
    first, second = tmp_path / "first", tmp_path / "second"
    assert invoke(inputs, first).exit_code == invoke(inputs, second).exit_code == 0
    assert artifacts(first) == artifacts(second)
    report, features = outputs(first, 10, 0, 0)
    assert report["ok"] and not report["fatal"] and report["roundtrip"]["preserved"]
    cells = read_cells(inputs["csv"])
    assert read_cells(first / "roundtrip.csv") == cells
    assert any("," in row[1] for row in cells[1:]) and any("\n" in row[5] for row in cells[1:])
    original = {f["id"]: f for f in json.loads(inputs["footprints"].read_text())["features"]}
    manifest = json.loads(inputs["provenance"].read_text())
    assert {f["id"] for f in features} == set(original)
    assert features[0]["properties"]["height_m"] == 24
    assert features[1]["properties"]["height_m"] == 32.5
    for feature in features:
        fid, props = feature["id"], feature["properties"]
        assert feature["geometry"] == original[fid]["geometry"]
        assert props["evidence_test_only"] is True and props["schematic"] is True and props["detailed"] is False
        for key in ("model_file", "provenance_file"):
            pointer = Path(props[key])
            assert not pointer.is_absolute() and ".." not in pointer.parts and (first / pointer).is_file()
        pv = json.loads((first / props["provenance_file"]).read_text())
        model = json.loads((first / props["model_file"]).read_text())
        digest = hashlib.sha256(json.dumps(feature["geometry"], sort_keys=True,
                                           separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        assert pv["geometry_sha256"] == model["extras"]["geometry_sha256"] == digest
        assert pv["test_only"] is model["extras"]["test_only"] is True
        assert pv["source_url"] == manifest[fid]["source_url"] == props["csv_source_pdf"]
        for key in ("page", "reviewer", "uncertainty", "footprint_method"):
            assert pv[key] == manifest[fid][key]
        assert pv["source_method"] == manifest[fid]["method"] and pv["height_method"] == props["height_method"]
        assert pv["resolved_height_m"] == model["extras"]["height_m"] == props["height_m"]
        assert pv["input_unit"] == manifest[fid]["unit"] and props["height_unit"] in {"m", "storeys"}
    for item in report["outputs"]:
        assert hashlib.sha256((first / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    for key in ("csv", "footprints", "provenance"):
        assert report["inputs"][key]["sha256"] == hashlib.sha256(inputs[key].read_bytes()).hexdigest()


def test_two_frozen_surrey_inputs_are_deterministic_local_conversions(tmp_path):
    directory = ROOT / "examples/csv2massing/surrey"
    files = {"csv": directory / "buildings.csv", "footprints": directory / "footprints.geojson",
             "provenance": directory / "provenance-manifest.json"}
    first, second = tmp_path / "first", tmp_path / "second"
    assert invoke(files, first).exit_code == invoke(files, second).exit_code == 0
    assert artifacts(first) == artifacts(second)
    _, features = outputs(first, 2, 0, 0, rows=2)
    assert {f["id"]: f["properties"]["height_m"] for f in features} == {
        "surrey-2354": 86.5, "surrey-132062": 107.5}


def test_bad_and_unknown_heights_keep_honest_partial_outputs(inputs, tmp_path):
    bad_out = tmp_path / "bad"
    result = invoke(inputs, bad_out, inputs["bad"])
    assert result.exit_code == 1 and "row 2" in result.output and "unitless_height" in result.output
    assert "Traceback" not in result.output and "internal error" not in result.output
    report, features = outputs(bad_out, 9, 0, 1)
    assert not report["ok"] and all(f["id"] != "synthetic-01" for f in features)
    cells = read_cells(inputs["csv"])
    cells[1][2] = "unknown"
    write_cells(inputs["csv"], cells)
    manifest = json.loads(inputs["provenance"].read_text())
    manifest["synthetic-01"]["unit"] = "unknown"
    write_json(inputs["provenance"], manifest)
    unknown_out = tmp_path / "unknown"
    result = invoke(inputs, unknown_out)
    assert result.exit_code == 1 and "missing_height" in result.output
    report, features = outputs(unknown_out, 9, 1, 0)
    props = next(f["properties"] for f in features if f["id"] == "synthetic-01")
    assert props["height_m"] is props["extrusion_height_m"] is props["model_file"] is None
    assert json.loads((unknown_out / props["provenance_file"]).read_text())["resolved_height_m"] is None
    assert not (unknown_out / "models/synthetic-01.gltf").exists() and not report["ok"]


@pytest.mark.parametrize("case,code", [
    ("missing_csv", "unreadable_file"), ("missing_footprints", "unreadable_file"),
    ("missing_provenance", "unreadable_file"), ("utf8", "not_utf8"),
    ("json", "malformed_footprints"), ("duplicate_json", "duplicate_id"),
    ("header", "header_mismatch"), ("crs", "wrong_crs"),
    ("polygon", "invalid_polygon"), ("point", "off_footprint"), ("duplicate_id", "duplicate_id"),
])
def test_invalid_inputs_have_plain_findings_and_nonzero_exit(inputs, tmp_path, case, code):
    if case.startswith("missing_"):
        inputs[case.removeprefix("missing_")].unlink()
    elif case == "utf8":
        inputs["csv"].write_bytes(b"\xff")
    elif case in {"json", "duplicate_json"}:
        inputs["footprints"].write_bytes(b'{"a":1,"a":2}' if case == "duplicate_json" else b"{broken")
    elif case == "header":
        cells = read_cells(inputs["csv"])
        cells[0][0] = "wrong"
        write_cells(inputs["csv"], cells)
    elif case == "point":
        document = json.loads(inputs["provenance"].read_text())
        document["synthetic-01"]["location_point"] = [-122.84, 49.20]
        write_json(inputs["provenance"], document)
    else:
        document = json.loads(inputs["footprints"].read_text())
        if case == "crs":
            document["crs"]["properties"]["name"] = "EPSG:26910"
        elif case == "polygon":
            document["features"][0]["geometry"]["coordinates"][0].pop()
        else:
            document["features"][1]["id"] = document["features"][1]["properties"]["id"] = "synthetic-01"
        write_json(inputs["footprints"], document)
    out = tmp_path / "result"
    result = invoke(inputs, out)
    assert result.exit_code == 1 and code in result.output and "Traceback" not in result.output
    report = json.loads((out / "validation.json").read_text())
    assert not report["ok"] and report["reconciliation"]["reconciled"]
    assert any(f["code"] == code and f["message"] for f in report["findings"])
    if case in {"polygon", "point"}:
        outputs(out, 9, 0, 1)
        assert not report["fatal"]
    elif case == "duplicate_id":
        outputs(out, 8, 0, 2)
        assert not report["fatal"]
    else:
        rows = 0 if case in {"missing_csv", "utf8"} else 10
        outputs(out, 0, 0, 0, not_evaluated=rows, rows=rows)
        assert report["fatal"]


@pytest.mark.parametrize("kind", ["nonempty", "file", "root", "symlink", "missing_parent"])
def test_unsafe_output_refused_before_mutation(inputs, tmp_path, kind):
    out = tmp_path / "destination"
    if kind == "nonempty":
        out.mkdir()
        (out / "sentinel").write_bytes(b"keep")
    elif kind == "file":
        out.write_bytes(b"keep")
    elif kind == "root":
        out = Path(tmp_path.anchor)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        out.symlink_to(target, target_is_directory=True)
    else:
        out = tmp_path / "absent" / "destination"
    def snapshot():
        return {p.relative_to(tmp_path).as_posix(): (p.lstat().st_ino, p.lstat().st_mtime_ns,
                p.read_bytes() if p.is_file() and not p.is_symlink() else None)
                for p in tmp_path.rglob("*")}
    before = snapshot()
    result = invoke(inputs, out)
    assert result.exit_code == 2 and "error:" in result.output and "Traceback" not in result.output
    assert snapshot() == before


def test_one_model_failure_rejects_entire_group_but_keeps_other_rows(inputs, tmp_path, monkeypatch):
    cells = read_cells(inputs["csv"])
    cells[1][3] = "2"
    del cells[2]
    write_cells(inputs["csv"], cells)
    document = json.loads(inputs["footprints"].read_text())
    document["features"][1]["properties"].update(name=cells[1][0], location=cells[1][1])
    write_json(inputs["footprints"], document)
    manifest = json.loads(inputs["provenance"].read_text())
    manifest["synthetic-02"].update(source_url=cells[1][6], unit="m")
    write_json(inputs["provenance"], manifest)
    original = cli.build_gltf
    def fail_one(building, pointer):
        if building.id == "synthetic-02":
            raise GltfError("invalid_model", "SYNTHETIC injected model failure")
        return original(building, pointer)
    monkeypatch.setattr(cli, "build_gltf", fail_one)
    out = tmp_path / "group"
    result = invoke(inputs, out)
    assert result.exit_code == 1 and "SYNTHETIC injected model failure" in result.output
    report, features = outputs(out, 8, 0, 1, rows=9)
    assert report["rows"][0]["status"] == "rejected"
    assert {f["id"] for f in features}.isdisjoint({"synthetic-01", "synthetic-02"})
    for fid in ("synthetic-01", "synthetic-02"):
        assert not (out / f"models/{fid}.gltf").exists() and not (out / f"provenance/{fid}.json").exists()


@pytest.mark.filterwarnings("error::RuntimeWarning")
def test_positive_height_that_collapses_float32_is_rejected_not_replaced(inputs, tmp_path):
    cells = read_cells(inputs["csv"])
    cells[1][2] = "0." + "0" * 299 + "1 m"
    write_cells(inputs["csv"], cells)
    out = tmp_path / "tiny"
    result = invoke(inputs, out)
    assert result.exit_code == 1 and "invalid_model" in result.output and "Traceback" not in result.output
    _, features = outputs(out, 9, 0, 1)
    assert all(f["id"] != "synthetic-01" for f in features)
    assert read_cells(out / "roundtrip.csv")[1][2] == cells[1][2]
