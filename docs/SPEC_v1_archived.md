# Frozen specification: massing-provenance-kit

Version 1.0, frozen 2026-10-06. Backbone: Plan A, Spec 2 (massing-kit). Compatible boundaries: Plan B, section 2. This document defines the complete scope; implementation batches may refine interfaces without adding features.

## Purpose and boundaries

An Apache-2.0 Python 3.11 toolkit converts public building footprints and explicit public height evidence into LOD1 prisms for MapLibre fill-extrusion and OBJ, with per-building provenance, honest labels and a QA report. It demonstrates a repeatable method for 80–90 buildings without claiming that such a selected portfolio has already been modelled or delivered. No City access, endorsement, client history, production integration, surveys or City-supplied drawings are claimed. Public data only; no credentials, analytics, runtime AI, deployment or publishing.

Detailed models are only a documented slot: a glTF filename associated with a stable building id. No glTF creation, rendering or detailed modelling feature. A future reviewed City-supplied override replaces an estimate; a test-only override must never be presented as real City evidence. An application site is not a building footprint. New-building placeholder sites, if used, must be explicitly identified as placeholder geometry and cannot silently replace surveyed footprints.

## Commands and outputs

| Command | Required behaviour |
|---|---|
| massing build | Read WGS84 GeoJSON footprints plus optional OSM, matched application-storey evidence and reviewed override CSV. Write massing.geojson, massing.obj, qa_report.md and labels.csv into an output directory. Support an offline fixture example and a bounded live public-data example. |
| massing match | Join application points or site polygons to footprints in EPSG:26910; write explicit matches and QA findings. Never infer project identity from a nearby building without reporting the spatial method. |
| massing override | Apply a reviewed CSV to existing massing features, preserve prior height evidence and regenerate all four outputs consistently. Reject invalid or unmatched override ids visibly. |

Every output feature has stable id, height_m, min_height_m, height_source, height_confidence, label_text and style_flag. Preserve footprint source_dataset, source_url, retrieved_at and licence separately from height evidence provenance. WGS84 geometry is ready for MapLibre without transformation. OBJ uses metre coordinates in EPSG:26910 with documented axes and a shared local origin. Source timestamps, hashes, query parameters, exclusion reasons and runtime/build information are recorded. Offline builds must be reproducible for fixed inputs; changing live data is explicitly outside that guarantee.

## Height evidence ladder

First usable evidence wins. Reject or report malformed, nonfinite, zero/negative heights and unsupported units rather than silently accepting them. Metres and explicitly supported conversions must be documented. min_height_m must be finite, nonnegative and below height_m.

| Source | Height rule | Confidence |
|---|---|---|
| city_supplied | Explicit reviewed override CSV height, evidence reference required; original estimate retained in height_prev_m | 1.0 |
| osm_height | Explicit OSM height in metres | 0.9 |
| osm_levels | Positive building:levels × 3.1 m + 1.5 m ground-floor allowance | 0.7 |
| application_storeys | Explicit positive storeys from public application text × 3.1 m + 1.5 m, retaining source text/reference | 0.6 |
| typology_default | Documented defaults by footprint area and explicit use class from configuration | 0.3 |
| unknown | Flat 4 m placeholder, explicitly uncertain | 0.1 |

Ten levels produces 32.5 m. Confidence is a declared evidence score, not a statistical probability. Low-confidence defaults and unknowns receive an uncertain style flag for a consuming map's dashed outline. label_rules.yaml defines exact approved wording for every source. Labels name the evidence and estimation status. Counts and QA must not hide weak evidence.

## Geometry, matching and QA

Use shapely and pyproj. Project to EPSG:26910 for all distance, area, simplification and extrusion calculations. Simplify at 0.3 m, repair validity explicitly, report changes and exclusions, drop slivers below 10 m². Preserve holes and polygon parts; reject unsupported geometry or explain exclusions. Stable ids must be unique and safely mapped to OBJ groups.

Polygon sites match intersecting footprints. Point sites consider footprints within 25 m ranked by distance, selecting the nearest where unambiguous. More than three candidates, no candidates and distance ties are QA findings instead of guesses. Conflicting project evidence must be visible. QA lists unmatched and ambiguous footprints/sites, repairs, dropped slivers, evidence failures and overrides, and reconciles input/output/excluded totals.

Extrude rings with outward wall normals and mapbox_earcut caps, including holes and MultiPolygon parts. For a simple n-vertex ring: 2n wall triangles and n−2 triangles per cap. Both bottom and top caps have correct opposing winding; holes are not filled. Bound vertex counts and assert indices are valid. No rendering dependency.

Flag height-to-footprint-width outliers and storey counts above 60; do not auto-correct these values. Document the width measure and ratio threshold. No silent correction of source measurements.

## Public sources

These exact official FeatureServer URLs supersede the older MapServer suggestions in the plans. Source selection comes from the existing demonstration SOURCES.json.

| Dataset | Endpoint | Licence |
|---|---|---|
| Building Footprints | https://services5.arcgis.com/YRpe0VKTJytZSSIB/arcgis/rest/services/Building%20Footprnts/FeatureServer/0 | Open Government License – City of Surrey |
| Development Applications | https://services5.arcgis.com/YRpe0VKTJytZSSIB/arcgis/rest/services/Development%20Applications/FeatureServer/0 | Open Government License – City of Surrey |
| OSM building height/levels | https://overpass-api.de/api/interpreter | © OpenStreetMap contributors, ODbL |

Surrey licence: https://opendata-surrey.hub.arcgis.com/pages/55089a19491a4fe59a41e059fd8af708. Verify live service metadata and applicable licensing before packaging public snapshots. Read-only bounded requests with timeout, paging/count integrity and explicit failure; cache Overpass responses with retrieval date. Do not fabricate records when services fail. Allowlist useful public attributes and omit applicant/contact details. Test geometry and fabricated override evidence are marked test-only. Original code/documentation is Apache-2.0; source data retains its separate licence and attribution.

## Stack and file responsibilities

Python 3.11 or later; shapely, pyproj, mapbox_earcut, typer, numpy, YAML support and a read-only HTTP client as needed. pytest with coverage, ruff, mypy, offline HTTP mocks. Pin direct dependencies and commit a generated dependency lock. Application modules: massing/__init__.py, cli.py, heights.py, match.py, simplify.py, extrude.py, labels.py, qa.py, osm.py; a bounded public-source I/O module may support the specified live smoke run. Configuration: config/typology_defaults.yaml and config/label_rules.yaml, packaged correctly for installed CLI use. Separate pure computations from I/O. No broader data-pipeline, transit scene, web app or renderer.

## Acceptance and validation

All tests run offline, blocking unmocked network access. Test ladder order/confidence; override priority and previous evidence; formula; unknown flag; polygon and point matching; ambiguity; projected simplification validity/area; prism counts; OBJ round-trip group/index counts; holes and polygon parts; every exact label rule; QA count reconciliation and >60 storey outlier; invalid evidence/ids/units; CLI build/match/override end-to-end; source adapter errors/cache/paging. At least 80% application coverage. Lint, type check and all tests pass. Installed CLI fixture smoke writes four consistent artefacts with non-null provenance and labels. A live read-only smoke against official Surrey services and OSM is recorded in docs/live-run.md with exact requests, timestamps, counts, hashes and actual success/failure; it cannot be substituted with fixtures.

## Documentation and release

README: purpose, commands, offline quickstart, real Surrey example, source/licence table, evidence ladder/formula, exact labels, matching/ambiguity rules, output contract, QA, limitations and what it does not do. Include an Open by design section covering inspectable source, reproducibility, public inputs and separate data licences; no production-security certification. Document sparse OSM heights, old footprints versus proposed buildings, massing rather than survey accuracy, and no real City overrides. Include the Plan A 60-second demo: ladder (0–10 s), build/QA (10–30), prototype label parity and uncertain outline if an external viewer is available (30–45), explicitly test-only override (45–55), provenance-preserving future detailed subset (55–60). CLI alone does not demonstrate a rendered prototype.

Include LICENSE (Apache-2.0, ParalleX Labs Inc.), CITATION.cff, source/dependency notices, and .github/workflows/ci.yml with permissions contents: read, offline verification, and no deployment. No GitHub repository creation or push. Commit locally as configured git user with plain messages. STATUS.md stays untracked and records delivered scope, every validation result, the private request ledger, review outcomes and anything incomplete.

## Authorship and review protocol

All application modules, tests, fixture loaders, CI and README are authored by the primary author; the independent test author writes tests and handles fallback after two failed primary fixes. Full-file batches use the required FILE marker plus fenced content; continue each author thread and attach this spec and relevant files on every request. Coordinator writes returned files byte for byte, captures public data, runs verification and sends exact failures back. Trivial local glue only after two failed author fix rounds, logged explicitly. Full tracked repository is packed, checked by the local confidentiality guard, then reviewed in two independent full-repository reviews for HIGH/MEDIUM/LOW findings. Repairs return through the coding authors, followed by complete verification.
