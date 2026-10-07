"""csv2massing: offline City CSV + explicit footprints + provenance -> schematic massing.

Usage: csv2massing CSV --footprints FILE --provenance FILE --out DIR
No network, clock or absolute paths in artefacts. The output directory must be absent or
empty (not a symlink, not a filesystem root); this is checked before anything is written and
every artefact is created exclusively, so nothing is overwritten. Artefact paths that differ
only by letter case are refused before the directory or any file is created, so they cannot
collide on case-insensitive filesystems. All artefacts are computed in memory first.
Exit codes: 0 every row accepted with a known height and a model; 1 data problems (rejected
rows, unknown heights, fatal input errors - artefacts and reports are still written);
2 unusable output directory or write failure; 3 unexpected internal error.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from typing import Annotated, Any

import typer

from massing import __version__
from massing.csvio import (
    CsvFinding,
    CsvParseResult,
    parse_csv_bytes,
    roundtrip_csv,
    roundtrip_preserved,
)
from massing.csvvalidate import (
    Building,
    RowOutcome,
    ValidationFinding,
    ValidationResult,
    parse_json_document,
    validate,
)
from massing.gltf import GltfError, GltfModel, build_gltf

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3
SCHEMATIC_NOTE = "Schematic prisms only; not detailed architectural models."

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False,
                  help="Convert a City CSV, explicit WGS84 footprints and a provenance "
                       "manifest into schematic massing GeoJSON, glTF and reports.")


class OutputDirError(Exception):
    """The output destination is unsafe or unusable."""


def check_output_dir(out: Path) -> None:
    """Refuse symlinks, non-directories, non-empty directories and filesystem roots."""
    try:
        if out.is_symlink():
            raise OutputDirError(f"output {out} is a symbolic link; choose a real directory")
        if out.exists():
            if not out.is_dir():
                raise OutputDirError(f"output {out} exists and is not a directory")
            if any(out.iterdir()):
                raise OutputDirError(f"output {out} is not empty; refusing to overwrite")
        elif not out.parent.is_dir():
            raise OutputDirError(f"parent directory of {out} does not exist")
        resolved = out.resolve()
    except OSError as exc:
        raise OutputDirError(f"cannot inspect output {out}: {exc.strerror or exc}") from exc
    if resolved == Path(resolved.anchor):
        raise OutputDirError("refusing to write into a filesystem root")


def provenance_path(building_id: str) -> str:
    return f"provenance/{building_id}.json"


def model_path(building_id: str) -> str:
    return f"models/{building_id}.gltf"


def _read(path: Path) -> tuple[bytes | None, str | None]:
    try:
        return path.read_bytes(), None
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc.strerror or exc}"


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _json_bytes(obj: object) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
            + "\n").encode("utf-8")


def _load_json(data: bytes | None, error: str | None, what: str
               ) -> tuple[object | None, tuple[ValidationFinding, ...]]:
    if data is None:
        return None, (ValidationFinding("unreadable_file", f"{what}: {error}"),)
    return parse_json_document(data, what)


def _finding_key(f: ValidationFinding) -> tuple[int, str, str, str]:
    return (f.row_number or 0, f.feature_id or "", f.code, f.message)


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    counts = report["reconciliation"]
    lines = ["# csv2massing validation", "",
             f"- Result: {'PASS' if report['ok'] else 'FAIL'}",
             f"- Tool: csv2massing {report['tool']['version']}",
             f"- Fatal input error: {'yes' if report['fatal'] else 'no'}",
             f"- {SCHEMATIC_NOTE}", "", "## Reconciliation", "", "| Measure | Count |",
             "|---|---|"]
    lines.extend(f"| {_cell(k)} | {_cell(v)} |" for k, v in sorted(counts.items()))
    lines += ["", "## Rows", "", "| Row | Status | Footprints |", "|---|---|---|"]
    lines.extend(f"| {r['row_number']} | {_cell(r['status'])} | {_cell(', '.join(r['feature_ids']))} |"
                 for r in report["rows"])
    lines += ["", "## Buildings", "", "| Id | Row | Height m | Model | Provenance |",
              "|---|---|---|---|---|"]
    lines.extend(f"| {_cell(b['id'])} | {b['row_number']} | {_cell(b['height_m'])} | "
                 f"{_cell(b['model_file'])} | {_cell(b['provenance_file'])} |"
                 for b in report["buildings"])
    lines += ["", "## Findings", ""]
    if not report["findings"]:
        lines.append("_None._")
    for f in report["findings"]:
        where = "".join([f"row {f['row_number']} " if f["row_number"] else "",
                         f"feature {f['feature_id']} " if f["feature_id"] else ""])
        lines.append(f"- [{f['severity']}] {_cell(where)}`{f['code']}`: {_cell(f['message'])}")
    return "\n".join(lines) + "\n"


def build_artefacts(csv_result: CsvParseResult, result: ValidationResult,
                    input_findings: tuple[ValidationFinding, ...],
                    inputs: dict[str, dict[str, object]]
                    ) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Compute every artefact in memory; extrusion failures reject their whole row."""
    models: dict[str, GltfModel] = {}
    failures: list[ValidationFinding] = []
    failed_rows: set[int] = set()
    for building in result.buildings:
        if not building.has_model:
            continue
        try:
            models[building.id] = build_gltf(building, provenance_path(building.id))
        except GltfError as exc:
            number = building.row.row_number
            failures.append(ValidationFinding(
                exc.code, f"row {number}: model for {building.id} could not be built ({exc}); "
                "the row is rejected and no height or geometry is invented", number, building.id))
            failed_rows.add(number)
    emitted: list[Building] = [b for b in result.buildings if b.row.row_number not in failed_rows]
    rows: list[RowOutcome] = [replace(r, status="rejected") if r.row_number in failed_rows else r
                              for r in result.rows]
    roundtrip = roundtrip_csv(csv_result)
    preserved = roundtrip_preserved(csv_result, roundtrip.text)
    extra: list[ValidationFinding] = []
    if not preserved:
        extra.append(ValidationFinding("roundtrip_mismatch",
                                       "roundtrip.csv does not reproduce the input cells"))
    extra.extend(ValidationFinding("roundtrip_omitted", f"row {n} was not seven cells and is "
                                   "not in roundtrip.csv", n, severity="note")
                 for n in roundtrip.omitted_rows)
    artefacts: dict[str, bytes] = {"roundtrip.csv": roundtrip.text.encode("utf-8")}
    features: list[dict[str, object]] = []
    building_rows: list[dict[str, object]] = []
    for building in emitted:
        model = models.get(building.id)
        prov_file = provenance_path(building.id)
        model_file = model_path(building.id) if model is not None else None
        record = building.provenance_record()
        record.update({"unit": record["input_unit"], "method": record["source_method"],
                       "provenance_file": prov_file, "model_file": model_file,
                       "model": model.summary() if model is not None else None})
        artefacts[prov_file] = _json_bytes(record)
        if model is not None and model_file is not None:
            artefacts[model_file] = model.text.encode("utf-8")
        features.append({"type": "Feature", "id": building.id,
                         "properties": building.geojson_properties(prov_file, model_file),
                         "geometry": building.footprint.geometry})
        building_rows.append({"id": building.id, "row_number": building.row.row_number,
                              "height_m": building.height_m, "model_file": model_file,
                              "provenance_file": prov_file})
    artefacts["massing.geojson"] = _json_bytes({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": features})
    status = Counter(r.status for r in rows)
    emitted_rows = {b.row.row_number for b in emitted}
    expected = sum(r.building_count or 0 for r in csv_result.rows
                   if r.row_number in emitted_rows)
    model_count = sum(1 for b in emitted if b.id in models)
    reconciled = (sum(status.values()) == len(csv_result.rows) and expected == len(emitted)
                  and model_count == sum(1 for b in emitted if b.has_model)
                  and result.reconciliation.reconciled)
    findings = sorted((*input_findings, *result.findings, *failures, *extra), key=_finding_key)
    ok = (not result.fatal and reconciled and bool(rows)
          and not any(f.severity == "error" for f in findings))
    report: dict[str, Any] = {
        "tool": {"name": "csv2massing", "version": __version__},
        "inputs": inputs, "ok": ok, "fatal": result.fatal,
        "reconciliation": {
            "csv_rows": len(csv_result.rows), "accepted_rows": status["accepted"],
            "unknown_height_rows": status["unknown_height"], "rejected_rows": status["rejected"],
            "not_evaluated_rows": status["not_evaluated"],
            "footprint_features": result.reconciliation.footprint_features,
            "output_buildings": len(emitted), "models_written": model_count,
            "unknown_height_buildings": sum(1 for b in emitted if not b.has_model),
            "provenance_files_written": len(emitted), "reconciled": reconciled},
        "validation_reconciliation": asdict(result.reconciliation),
        "rows": [asdict(r) for r in rows], "buildings": building_rows,
        "findings": [asdict(f) for f in findings],
        "roundtrip": {"written_rows": list(roundtrip.written_rows),
                      "omitted_rows": list(roundtrip.omitted_rows), "preserved": preserved},
        "outputs": [{"path": p, "sha256": _sha(d)} for p, d in sorted(artefacts.items())],
    }
    artefacts["validation.json"] = _json_bytes(report)
    artefacts["validation.md"] = render_markdown(report).encode("utf-8")
    return artefacts, report


def write_artefacts(out: Path, artefacts: dict[str, bytes]) -> None:
    check_output_dir(out)
    folded = Counter(relative.casefold() for relative in artefacts)
    colliding = sorted(relative for relative in artefacts if folded[relative.casefold()] > 1)
    if colliding:
        raise OutputDirError(
            f"artefact paths {colliding} differ only by letter case and would overwrite each "
            "other on a case-insensitive filesystem; nothing was written")
    out.mkdir(exist_ok=True)
    for relative, data in sorted(artefacts.items()):
        target = out / relative
        target.parent.mkdir(exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)


def run(csv_path: Path, footprints_path: Path, provenance_path_: Path, out: Path) -> int:
    try:
        check_output_dir(out)
    except OutputDirError as exc:
        typer.echo(f"error: {exc}", err=True)
        return EXIT_USAGE
    csv_bytes, csv_error = _read(csv_path)
    csv_result = (parse_csv_bytes(csv_bytes) if csv_bytes is not None else
                  CsvParseResult(None, (), (CsvFinding("unreadable_file", f"CSV: {csv_error}"),)))
    fp_bytes, fp_error = _read(footprints_path)
    pv_bytes, pv_error = _read(provenance_path_)
    fp_obj, fp_findings = _load_json(fp_bytes, fp_error, "footprints")
    pv_obj, pv_findings = _load_json(pv_bytes, pv_error, "provenance")
    result = validate(csv_result, fp_obj, pv_obj)
    inputs: dict[str, dict[str, object]] = {
        "csv": {"name": csv_path.name, "sha256": _sha(csv_bytes)},
        "footprints": {"name": footprints_path.name, "sha256": _sha(fp_bytes)},
        "provenance": {"name": provenance_path_.name, "sha256": _sha(pv_bytes)}}
    artefacts, report = build_artefacts(csv_result, result, (*fp_findings, *pv_findings), inputs)
    try:
        write_artefacts(out, artefacts)
    except (OSError, OutputDirError) as exc:
        typer.echo(f"error: could not write outputs: {exc}", err=True)
        return EXIT_USAGE
    for f in report["findings"]:
        if f["severity"] == "error":
            where = f"row {f['row_number']}: " if f["row_number"] else ""
            typer.echo(f"error: {where}{f['message']} [{f['code']}]", err=True)
    counts = report["reconciliation"]
    typer.echo(f"csv2massing: {counts['accepted_rows']} accepted, "
               f"{counts['unknown_height_rows']} unknown-height, {counts['rejected_rows']} "
               f"rejected, {counts['not_evaluated_rows']} not evaluated of {counts['csv_rows']} "
               f"rows; {counts['output_buildings']} buildings, {counts['models_written']} models "
               f"written to {out}")
    return EXIT_OK if report["ok"] else EXIT_INVALID


@app.command()
def main(
    csv_file: Annotated[Path, typer.Argument(metavar="CSV", help="City CSV, seven columns.")],
    footprints: Annotated[Path, typer.Option("--footprints", metavar="FILE",
                                             help="EPSG:4326 footprint FeatureCollection.")],
    provenance: Annotated[Path, typer.Option("--provenance", metavar="FILE",
                                             help="Provenance manifest JSON keyed by id.")],
    out: Annotated[Path, typer.Option("--out", metavar="DIR",
                                      help="New or empty output directory.")],
) -> None:
    """Convert CSV rows bound to explicit footprints into schematic massing artefacts."""
    try:
        code = run(csv_file, footprints, provenance, out)
    except Exception as exc:  # plain message instead of a traceback
        typer.echo(f"internal error: {type(exc).__name__}: {exc}", err=True)
        code = EXIT_INTERNAL
    raise typer.Exit(code)
