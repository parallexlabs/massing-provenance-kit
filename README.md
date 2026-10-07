# csv2massing

`csv2massing` converts a seven-column City CSV, explicitly supplied building footprints and a provenance manifest into schematic building prisms. It writes GeoJSON, glTF 2.0, per-building provenance and validation reports. The active contract is [SPEC v2](SPEC.md). The earlier massing-provenance-kit workflow is kept in [the archived specification](docs/SPEC_v1_archived.md).

Copyright 2026 ParalleX Labs Inc., Apache-2.0. No public repository, package release, City approval, human review, ArcGIS Online upload or publication is claimed.

## Status

The earlier two-building Surrey run is recorded in [docs/live-run.md](docs/live-run.md). It accepted 2 of 2 rows and wrote 2 original-geometry GeoJSON features, 2 glTF models and 2 provenance records, and all counts reconciled.

Final local gates, run after the review repairs:

- **Tests:** 697 passed and 0 failed offline with sockets disabled. Branch-inclusive coverage was 95.73%, against a minimum of 80%.
- **Review regressions:** all 48 new regression tests pass. On the exact pre-review sources, 43 of them fail and 5 pass, so they exercise the repaired defects.
- **Static checks:** Ruff passes on the whole repository, and strict mypy passes on all 13 source files.
- **Byte stability:** in a fresh checkout under `core.autocrlf`, all 27 data and lock files are byte-identical.
- **Conversion:**
  - The ten synthetic rows convert with their original geometry.
  - `bad.csv` exits 1 and still converts the nine valid rows.
  - 24 deterministic artifacts compare byte-identical across repeat runs.
  - The two real buildings' output files are unchanged byte for byte.
- **glTF:** all 12 freshly generated models passed the Khronos glTF Validator with zero errors, warnings, infos and hints.
- **Installed wheel:** the CI steps ran outside the checkout with the network blocked. The good run produced 10 buildings, the bad run 9, the repeat run was byte-identical, and `pip check` passed.
- **Reviews:** two static repository reviews were completed, one round each. Both returned REPAIR with no HIGH findings. Accepted findings were fixed through coding-author patches and checked by the gates above. This is not an independent post-patch review or approval.

Not executed: GitHub-hosted CI, human sign-off of the evidence, City approval, and any ArcGIS conversion, upload or publication.

## Install

Use Python 3.11 and the committed `requirements.lock`, which has 33 exact pins covering runtime, test, lint, type-check and build dependencies (including `setuptools==80.10.2` and `wheel==0.48.0`). Install locked dependencies first, then the package itself without resolving dependencies:

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
csv2massing --help
```

The lock is regenerated reproducibly with:

```sh
uv pip compile pyproject.toml --extra dev --constraint requirements.lock --output-file requirements.lock
```

Installing dependencies may contact package indexes. The conversion itself never makes a network call: no geocoding, PDF download or remote request.

## Command

There is one command and no subcommand:

```sh
csv2massing CSV --footprints FILE --provenance FILE --out DIR
```

`--out` must be a new or empty directory, and its parent directory must already exist.

Anything else is refused before anything is written:
- a missing parent;
- a non-empty directory, including a re-run into a directory from an earlier run;
- an existing file;
- a symlink;
- a filesystem root.

Artifact paths that would differ only by letter case are also refused before the directory or any file is created.

No artifact is ever overwritten. To re-run, choose a new directory name.

Exit codes:
- **0:** every row was accepted with a known height and a model.
- **1:** data problems, such as rejected rows, unknown heights or a malformed input file. Reports are still written.
- **2:** unusable output directory, including a missing parent, or a write failure.
- **3:** unexpected internal error, shown as one plain line instead of a traceback.

## Input contract

The CSV is UTF-8 with RFC 4180 quoting and exactly these seven headers, in this order:

```csv
name,location,height,building_count,floor_area,status,source_pdf
```

| Column | Interpretation |
| --- | --- |
| `name` | Exact match to the footprint's `properties.name` |
| `location` | Exact match to `properties.location`; a supplied address or coordinate string, never geocoded |
| `height` | Positive finite number with explicit `m`, `metres` or `meters`; or positive whole number with `storeys` or `stories` (singular also accepted); blank or `unknown` means unknown |
| `building_count` | Positive whole number equal to the distinct physical buildings bound to the row |
| `floor_area` | Nonnegative square metres (optional `m2`), blank or `unknown`; kept whole as the group's value |
| `status` | Copied verbatim |
| `source_pdf` | `http(s)` URL with a host, or `test-only://` for synthetic data; must equal each bound provenance `source_url`; never fetched |

Metre heights are used as written: `24 m` stays 24 m, with no multiplier. Storey heights use the documented schematic rule:

```text
height_m = 4.0 + 3.0 * (storeys - 1) + 1.5
10 storeys = 32.5 m
```

These allowances are schematic, not measured dimensions. Unitless values, non-finite or non-positive heights and fractional storeys are rejected. An unknown height has no fallback and no 4 m placeholder.

The footprint and provenance files must be strict UTF-8 JSON, and anything else is a malformed-input error. The following are rejected:
- duplicate keys;
- NaN or Infinity;
- numbers that overflow;
- invalid Unicode text, anywhere in the document.

**Footprint file:** a GeoJSON FeatureCollection with an explicit `{"crs":{"type":"name","properties":{"name":"EPSG:4326"}}}` declaration.
- **Geometry:** each feature must be an already valid, explicitly closed Polygon or MultiPolygon with finite WGS84 coordinates and positive area. Nothing is repaired or simplified, and holes and multipart geometry are preserved.
- **Identifiers:** `Feature.id` must be a filename-safe string and equal `properties.id`. Optional `physical_building_id` defaults to the id.
  - Feature ids must be unique ignoring letter case. Ids that differ only in case, such as `Tower-A` and `tower-a`, are all rejected as `duplicate_id` before any output is written, because their file names would collide on case-insensitive filesystems.
  - Ids are never renamed or lower-cased.
  - Duplicate `physical_building_id` values are compared exactly, with case significant.
- **Invalid ids:** a feature whose id is missing or unsafe still binds by its name and location, so the row it belongs to is rejected rather than passing without it.
- **Errors:** a missing or wrong CRS, duplicate ids, duplicate physical ids and positively overlapping separate footprints are all errors.

**Provenance manifest:** keyed by footprint id. Each entry has:
- `location_point` as `[longitude, latitude]`, which must lie within or on its polygon;
- `source_url` and a positive one-based PDF `page`;
- `unit`, which must match the row's height unit;
- `method`, `reviewer` and `uncertainty`;
- `footprint_source_url` and `footprint_method`;
- `schematic: true` and `detailed: false`;
- a boolean `test_only`, which must be `true` whenever any URL is `test-only://`.

An unknown reviewer must be stated explicitly, for example as unreviewed, and is never attributed to a person or the City. Extra keys are kept.

**Grouped rows:** each bound member receives the row height, and this uniform-height assumption is recorded. Floor area is not divided among members. A count of two never duplicates one footprint. A tower and its podium are one reviewed physical-building footprint, and their storeys are never added together.

## Outputs

| Artifact | Contents |
| --- | --- |
| `massing.geojson` | One feature per output physical building; unmodified original WGS84 geometry, the CSV cells, group count and floor area, height fields, evidence fields and pointers |
| `roundtrip.csv` | Same seven headers and unchanged cell values (quoting and line endings may differ) |
| `validation.json` / `validation.md` | Row, input, accepted, unknown, rejected, building and model counts with reconciliation, plus every finding with its row number |
| `provenance/{id}.json` | Source URL, page, unit, method, raw and resolved height, height method, reviewer, uncertainty, status, group values, footprint source/method, geometry hash, flags and extras |
| `models/{id}.gltf` | One self-contained glTF 2.0 prism per building with a known height |

GeoJSON properties include:
- `height_m`, `base_height_m=0`, `extrusion_height_m`;
- `height_raw`, `height_unit`, `height_method`;
- `schematic=true`, `detailed=false`;
- `provenance_file` and `model_file`.

`provenance_file` and `model_file` are relative pointers, resolved against the output directory root.

A valid row with unknown height still gets its GeoJSON feature and provenance record, with JSON null height and `model_file=null`. It gets no glTF, reports `missing_height` and makes the exit code non-zero. Other invalid rows are rejected with their row number and a plain-language reason. If a model can't be built, its whole row is rejected and nothing is invented.

Model building is conservative. If float32 positions would collapse any triangle to zero area (for example, from a vanishingly small height), the model is rejected as `invalid_model` instead of being rounded or given a minimum height.

**glTF models:** triangles in one embedded buffer (float32 positions, uint32 indices), in metres, right-handed Y-up (east, up, negative north), with winding preserved.
- **Origin:** each building has a deterministic EPSG:26910 origin and WGS84 anchor, stored in glTF `extras` with its id, provenance pointer and schematic/detailed flags.
- **Georeferencing:** GIS software doesn't read extras automatically, so georeferencing must be applied and checked when the model is imported.
- **Base elevation:** base zero is local ground, not a surveyed elevation.
- **Determinism:** artifacts contain no clock time and no output-directory path, so fixed inputs give byte-identical outputs.

## Synthetic examples

The ten-row synthetic sample in `examples/csv2massing/synthetic` is test-only. It has ten distinct supplied footprints, `test-only://` evidence and `test_only: true`. Row 1 is `24 m` and row 2 is `10 storeys`.

Create the parent directory for the example outputs first. A clean checkout has no `output/` directory, and without it every command below would be refused with exit code 2:

```sh
mkdir -p output
```

```sh
csv2massing examples/csv2massing/synthetic/buildings.csv \
  --footprints examples/csv2massing/synthetic/footprints.geojson \
  --provenance examples/csv2massing/synthetic/provenance-manifest.json \
  --out output/synthetic
```

Expected: exit 0, with ten features, ten models and ten provenance records.

`bad.csv` is identical except that the first data row (CSV row 2) has the unitless height `24`:

```sh
csv2massing examples/csv2massing/synthetic/bad.csv \
  --footprints examples/csv2massing/synthetic/footprints.geojson \
  --provenance examples/csv2massing/synthetic/provenance-manifest.json \
  --out output/bad
```

Expected:
- **Exit code:** 1.
- **Terminal error:** a plain `unitless_height` error for row 2.
- **Outputs:** nine features, nine models and nine provenance records.
- **Validation report:** row 2 is listed as rejected.

Running any of these commands a second time with the same `--out` is refused with exit code 2, because the directory is no longer empty. Use a new directory name instead.

## Two real Surrey buildings

```sh
csv2massing examples/csv2massing/surrey/buildings.csv \
  --footprints examples/csv2massing/surrey/footprints.geojson \
  --provenance examples/csv2massing/surrey/provenance-manifest.json \
  --out output/surrey
```

| Id | Building | Input | Schematic | Evidence (one-based page) | PDF SHA-256 |
| --- | --- | --- | --- | --- | --- |
| `surrey-2354` | Wave Tower 1, 13303 Central Avenue | 28 storeys | 86.5 m | [PLR_7915-0375-00](https://www.surrey.ca/sites/default/files/planning-reports/PLR_7915-0375-00.pdf), page 3 | `577c18fac2b86f7f9505af5de999fddbeb4706b5eacb4995d455f06b9ddfc081` |
| `surrey-132062` | Ultra Tower, West Village Phase 3, 13325 102A Avenue | 35 storeys | 107.5 m | [PLR_7917-0011-00](https://www.surrey.ca/sites/default/files/planning-reports/PLR_7917-0011-00.pdf), page 5 | `76590343d79f5896f410564e66b303ff4003a22201e5fb823e64855c18a590d3` |

- **Footprints:** unchanged official geometry from the Surrey Building Footprints FeatureServer, OBJECTIDs 2354 and 132062. The exact two-id response SHA-256 is `eb300ddd633ba65b1a74a3518f91958031849db1fe188ca28c7d5d3a8dd08398`.
- **Manifest geometry hashes:** the `footprint_geometry_sha256` values recorded in the manifest are `91df5a1a…6982` (2354) and `15a54268…115d` (132062).
- **Identity:** traced through PDF page 21 of PLR_7917-0011-00 (master plan), with OSM ways 1206021168 and 390951278 as corroboration.
- **Disclosed discrepancy:** OSM lists Wave as 27 levels plus 2 roof levels. The planning report's 28 storeys is used and the difference is kept, not reconciled.
- **What these heights are:** historical planning storey counts converted to schematic heights, not measured heights. Floor area is unknown and left blank, and the reviewer field says unreviewed.

See [the evidence trace](docs/real-building-evidence.md) before interpreting these models.

## GIS handoff

[docs/arcgis-online-conversion.md](docs/arcgis-online-conversion.md) describes the City GIS conversion step using official Esri documentation, with two paths:
- **Hosted feature layer:** publish `massing.geojson` as a hosted feature layer ([publish features](https://doc.arcgis.com/en/arcgis-online/manage-data/publish-features.htm)). Then extrude it in Scene Viewer by `height_m` in metres ([style polygons](https://doc.arcgis.com/en/arcgis-online/create-maps/scene-style-polygons.htm)).
- **Per-building glTF:** import each model into ArcGIS Pro with [Import 3D Objects](https://pro.arcgis.com/en/pro-app/latest/tool-reference/data-management/import-3d-objects.htm), into a 3D object feature class defined in EPSG:26910, applying the recorded EPSG:26910 offsets.

City GIS staff perform any conversion, permission checks and publishing. None of that has been executed here.

## Open by design

All inputs, formulas, reports, GeoJSON, CSV, provenance JSON and glTF are inspectable plain files. Conversion is offline and deterministic for fixed inputs.

These are schematic massing prisms, not detailed architectural models, surveys or facades. Out of scope:
- SLPK or I3S export;
- PDF extraction;
- deployment, rendering or runtime AI;
- automatic publishing;
- any claim of a delivered 90-model portfolio.

Legacy modules keep their archived contracts and tests. Their evidence ladder, typology defaults and 4 m unknown placeholder are not part of the CSV height contract.

Code, documentation and authored synthetic fixtures are Apache-2.0 ([LICENSE](LICENSE)). Source data keeps its own terms:
- **Surrey footprints:** the [Open Government License – City of Surrey](https://opendata-surrey.hub.arcgis.com/pages/55089a19491a4fe59a41e059fd8af708).
- **OSM corroboration:** © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright).
- **Planning PDFs:** referenced by URL and hash, not redistributed.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Quality gates

```sh
ruff check .
mypy --strict massing
pytest
```

`pytest` uses the project settings: tests run offline with sockets disabled and must reach at least 80% branch-inclusive coverage.

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs these gates on Python 3.11 from the lock, then:
1. Installs dependencies from the lock; this step may use package indexes.
2. Builds the local wheel without resolving dependencies.
3. Installs the wheel into a separate environment.
4. Runs the installed-CLI smoke outside the checkout with sockets blocked: the good synthetic run, the bad-row run and a repeated byte-identical artifact check.

CI only verifies; it doesn't release or deploy.
