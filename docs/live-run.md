# Live public-service run: 2026-10-06

Public source acquisition and the narrow csv2massing two-building smoke succeeded. The earlier bounded source and geometry checks below are retained as acquisition evidence; the active v2 delivery is the two-building CSV example, not a 90-model portfolio. Post-review core gates pass: 697 offline tests, 95.73% branch-inclusive coverage, whole-repository Ruff and strict typing. Both single static review rounds are complete; accepted repairs were verified locally.

## Acquisition

Bounding box (west, south, east, north): `-122.857,49.178,-122.843,49.191`, WGS84. Both official FeatureServers reported OBJECTID and EPSG:26910 in their layer metadata. Queries used `where=1=1`, envelope intersection, WGS84 output and allowlisted attributes. Exact encoded query URLs, parameters, retrieval timestamps and raw-response/snapshot SHA-256 hashes are in [SOURCES.json](../examples/surrey/SOURCES.json).

| Public source | Actual UTC retrieval | Captured | Total records intersecting tile | Selection |
|---|---|---:|---:|---|
| Surrey Building Footprints | 2026-10-06T20:03:04.251193+00:00 | 90 | 692 | First90 ordered by OBJECTID |
| Surrey Development Applications | 2026-10-06T20:03:04.899858+00:00 | 200 | 335 | First200 ordered by OBJECTID |
| OpenStreetMap Overpass ways | 2026-10-06T20:04:12.119610+00:00 | 89 | Not independently inventoried | Building ways with height or building:levels in tile |

Both GeoJSON service responses included `properties.exceededTransferLimit: true` inside the collection properties. Separate count and id requests verified the full tile totals and bounded selections independently of that nested flag. The manifest records the flag location and selection bounds. These are demonstration subsets, not a complete inventory or a selected contract portfolio. Applicant/contact fields were omitted. OSM address tags were omitted. Native source geometry was retained.

A second live check at21:48UTC used returnIdsOnly against both official services:692 unique footprint ids and335 unique site ids. The recorded90/200 selections exactly matched the first sorted ids at that time. [IDS_CHECK.json](../examples/surrey/IDS_CHECK.json) records the actual request URLs, retrieval timestamps and response hashes. No missing or duplicate ids were observed in this bounded selection check.

Surrey source data retains the [Open Government License – City of Surrey](https://opendata-surrey.hub.arcgis.com/pages/55089a19491a4fe59a41e059fd8af708). OSM data is © OpenStreetMap contributors under [ODbL](https://www.openstreetmap.org/copyright). Licence verification used the official Surrey ArcGIS Hub page item data and OSM copyright page. Code licensing does not replace data licensing. The raw Surrey BUILDING_HEIGHT field is retained as public input but is outside the frozen height-evidence ladder; it is not treated as a reviewed City override.

## Geometry integration observation

All90 footprint geometries and all200 site geometries were valid at capture; six sites were MultiPolygons. The repaired geometry implementation prepared90 footprints, excluded0 and reconciled totals. More-than-three-candidate sites produced QA findings without matches. Public footprint18570 rejected a simplification that would have changed area by4.847889%; the retained geometry changed area by0% and recorded the reason. The additional attributed geometry regression file records this source observation.

Python3.11.11 on macOS arm64 with exact dependency versions recorded in requirements.lock. No service write, credentials, real City override or rendered viewer was used.

## Application smoke

The active v2 inputs are in [examples/csv2massing/surrey](../examples/csv2massing/surrey). An exact two-ID public footprint query captured at 2026-10-06T22:47:39 UTC returned OBJECTIDs 2354 and 132062, with original coordinates preserved. Its response SHA-256 is `eb300ddd633ba65b1a74a3518f91958031849db1fe188ca28c7d5d3a8dd08398`; query, source fields and retrieval evidence are in that example's SOURCES.json.

The assembled CLI ran at 23:54 UTC with buildings.csv, footprints.geojson and provenance-manifest.json. It accepted two rows, emitted two original-geometry GeoJSON features, two glTFs and two provenance records, and reconciled all counts. Wave's 28 storeys became schematic 86.5 m; Ultra's 35 storeys became schematic 107.5 m. Source PDF pages, units, methods, uncertainty and explicitly unreviewed attribution remained in per-building provenance. All seven CSV cells round-tripped unchanged. These are documentary storey counts converted to schematic heights, not measured metre heights or approved models.

Both generated models passed Khronos glTF Validator 2.0.0-dev.3.10 with zero errors, warnings, infos and hints. An initial installed-wheel smoke ran outside the checkout with a Python audit hook blocking socket access; packaged configuration resources resolved from site-packages and every artifact was byte-identical to the editable CLI run. A separate unknown-height test retained null height and provenance, emitted no model for that building, reported missing_height and exited nonzero. A nonempty output directory was refused before any artifact changed.

These are actual local integration checks. The ten-row synthetic acceptance run subsequently passed: ten original-geometry features, ten models, ten provenance records and all seven raw CSV cells retained, including the comma and quoted newline. A repeat run produced all24artifacts byte-for-byte identically. The deliberate unitless-height row was rejected with the plain error “height '24' has no unit; write e.g. '24 m' or '10 storeys'”; nine other rows converted and counts reconciled. The synthetic 24m input remained24m and10storeys resolved32.5m. Independent decoded-model checks verified positive triangle areas, roof/floor winding and roof area matching original projected footprints, including the hole and multipart examples. All twelve synthetic/real models passed Khronos with zero errors, warnings, infos and hints. A numerical edge-case repair rejects positive1e-300m that collapses to zero in float32 and1e39m that overflows float32, with plain invalid_model errors; all eight real-building artifacts remain byte-identical after that validation repair.

The repaired wheel passed the exact authored CI installed-CLI smoke outside the checkout with network blocked: ten good models, nine valid models after the deliberately bad row, and deterministic repeat artifacts. Whole-repository post-review coverage and review outcomes are recorded below. No ArcGIS upload or City GIS conversion has been executed.

## Returned source-adapter smoke

The assembled source adapters fetched90footprints and200application sites successfully using live metadata, returnIdsOnly, independent count validation and bounded objectId pages. The first Overpass GET returned HTTP504; a later explicit retry at2026-10-06T22:16:19.382150+00:00 succeeded, retrieving89ways at22:16:23.362359UTC. Response SHA-256: `be418231363c2241449a2e1a68e781f989585fd5954e86ba67b79e7667c9579d`. All request parameters, dates, counts, hashes and the initial failure are in [ADAPTER_RUN.json](../examples/surrey/ADAPTER_RUN.json). No fixtures were substituted for any successful live request.

The Overpass query combines height and building:levels ways and limits output to5001elements; the parser rejects more than5000 as a truncation failure. Its output modifiers follow the [official Overpass QL documentation](https://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_QL#out). The cache records and checks query/payload hashes and retrieval date. Real source geometries in the earlier frozen snapshots produced14unique strong OSM matches, with at least80%overlap of both footprint and way;75other ways produced explicit findings.

The old source acquisition is separate from the v2 CLI smoke above. Whole-source lint and strict type checks pass, and the complete offline suite passes 697 tests. Final request receipts, package hashes and commit evidence are recorded in the untracked STATUS.md; these are not inferred from successful HTTP responses.

## Post-review verification (2026-10-07 UTC; 2026-10-06 PT)

The complete offline suite passes 697 tests with 95.7256% branch-inclusive coverage against an 80% gate. Whole-repository Ruff and strict mypy across all 13 application modules pass. The 48 independent review regressions pass on the repaired sources; on the exact preceding independently authored validator and CLI, the same tests produced 43 expected failures and five passes in an isolated temporary source checkout.

The repairs reject JSON exponent overflow and lone Unicode surrogates at the input boundary, retain invalid-ID features in their row's rejection group, reject casefold feature-ID collisions while preserving exact physical-ID comparison, and refuse colliding artifact paths before creating files. Git attributes preserve all 27 data/lock files byte-for-byte in a fresh checkout with autocrlf and CRLF conversion enabled.

Fresh CLI runs pass all ten raw-cell round-trips, unchanged original geometry, 24 m identity conversion, 10-storey conversion to 32.5 m, plain bad-row exit 1 with nine valid buildings, and 24 byte-identical repeated artifacts. All eight real-building output artifacts remain byte-identical to the checked public bundle. All twelve freshly generated glTFs pass Khronos with zero errors, warnings, infos and hints. Installed-wheel smoke, packaged configuration and dependency checks pass locally; dependency installation and the network-blocked CLI smoke are separate steps.

Two independent reviewers completed one static review round each, with remainder continuations to finish disclosed file coverage. No HIGH findings were identified. Their final static verdicts were REPAIR; accepted validation, writer, Git-attribute, GIS-instruction and documentation repairs were carried through the coding authors and coordinator gates. The reviewers did not execute this repository or perform an independent post-patch re-review. The corrected GIS instructions use actual output fields and EPSG:26910 offsets before any subsequent reprojection.

Human evidence sign-off, City approval, ArcGIS conversion/upload/publication and GitHub-hosted CI have not been executed. No push or repository creation is part of this local build.
