# Third-party notices

Original code, documentation and authored synthetic fixtures: copyright 2026 ParalleX Labs Inc., Apache-2.0 ([LICENSE](LICENSE)). That licence does not cover third-party software, source data or source documents.

## Data

| Material | Attribution and terms |
| --- | --- |
| Surrey building footprints | Contains information licensed under the [Open Government License – City of Surrey](https://opendata-surrey.hub.arcgis.com/pages/55089a19491a4fe59a41e059fd8af708). Geometry is used unchanged; source, licence and retrieval records are kept in `examples/csv2massing/surrey/SOURCES.json` and the provenance manifest. Raw `BUILDING_HEIGHT` is not used as height evidence. |
| Surrey planning reports | Cited by URL, one-based page and SHA-256; the PDFs are not redistributed in this repository. Public availability does not by itself grant redistribution rights. |
| OpenStreetMap | © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright). Used only as identity corroboration, kept separate from official geometry and planning-report storey evidence. |
| Generated outputs from real inputs | Keep the underlying data attribution; the Apache-2.0 code licence does not relicense source geometry or derived data. |
| Synthetic fixtures | Authored test-only data, Apache-2.0, not City or OSM records. |
| Esri documentation | External links only; not part of this project. |

## Python distributions in `requirements.lock`

All 33 distributions are installed, not bundled in this repository. Versions match the lock. Licence values come from installed metadata captured on Python 3.11.11 (macOS arm64); the `setuptools` and `wheel` values come from their supplied build-tool metadata.

| Distribution | Version | Declared licence |
| --- | --- | --- |
| anyio | 4.15.1 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| click | 8.5.0 | BSD-3-Clause |
| coverage | 7.16.2 | Apache-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| iniconfig | 2.3.0 | MIT |
| mapbox-earcut | 1.0.3 | ISC (classifier) |
| markdown-it-py | 4.2.0 | MIT (classifier) |
| mdurl | 0.1.2 | MIT (classifier) |
| mypy | 1.15.0 | MIT |
| mypy-extensions | 1.1.0 | MIT |
| numpy | 2.2.6 | BSD (NumPy licence text with bundled-component notices) |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pluggy | 1.6.0 | MIT |
| pygments | 2.21.0 | BSD-2-Clause |
| pyproj | 3.7.1 | MIT |
| pytest | 8.3.5 | MIT |
| pytest-cov | 6.1.1 | MIT |
| pytest-socket | 0.7.0 | MIT |
| pyyaml | 6.0.2 | MIT |
| respx | 0.22.0 | BSD-3-Clause |
| rich | 15.0.0 | MIT |
| ruff | 0.11.8 | MIT |
| setuptools | 80.10.2 | MIT |
| shapely | 2.1.1 | BSD 3-Clause |
| shellingham | 1.5.4 | ISC |
| typer | 0.16.0 | MIT (classifier) |
| types-pyyaml | 6.0.12.20241230 | Apache-2.0 |
| typing-extensions | 4.16.0 | PSF-2.0 |
| wheel | 0.48.0 | MIT |

Note that certifi is MPL-2.0, not MIT.

## Native components

The captured NumPy wheel's licence text includes notices for bundled components:
- **Notices included:** lapack-lite, dragon4, libdivide, OpenBLAS/LAPACK and GCC runtime pieces.
- **Terms named:** BSD-family, MIT, Zlib, and GPL with the GCC Runtime Library Exception.

Wheel contents vary by platform, so keep each installed wheel's own licence files.

The native libraries inside the shapely and pyproj wheels weren't inventoried for this file. Their installed metadata alone doesn't list them.
