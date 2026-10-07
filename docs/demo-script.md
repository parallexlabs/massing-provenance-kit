# 60-second csv2massing demo

This is a rehearsal script. The two-building Surrey run was observed locally (see [live-run.md](live-run.md)). Rehearse the synthetic commands before recording, and narrate only the counts you actually see.

## Prepare

Do this before the timed narration.

1. Install from the lock as in the [README](../README.md).
2. Confirm `csv2massing --help` shows the positional `CSV` argument and the `--footprints`, `--provenance` and `--out` options.
3. From the repository root, create the output parent directory. A clean checkout doesn't have it, and the CLI refuses an `--out` whose parent is missing (exit code 2):

```sh
mkdir -p output
```

A non-empty output directory is also refused, so a re-run into `output/demo-*` from an earlier rehearsal will fail. Don't delete earlier outputs. Use new names instead, such as `output/demo2-synthetic`, and change all three commands and the count loop to match.

If any command misbehaves, stop and report it. Never substitute invented output.

## Timed narration

| Time | Action and narration |
| --- | --- |
| 0–10 s | Show the seven CSV headers. "Offline, explicit inputs. `24 m` stays 24 metres. `10 storeys` uses the documented schematic rule, 4 + 3 × 9 + 1.5, which is 32.5 metres. Unknown height stays null; there is no fallback." |
| 10–25 s | Run the ten-row synthetic sample and print its counts. "Ten test-only footprints, ten models, ten provenance records. Synthetic evidence, not a City project." |
| 25–40 s | Open the Surrey outputs. "Two historical public planning storey counts, 28 and 35, become schematic heights of 86.5 and 107.5 metres. The PDF page and official footprint ID are in each provenance file. Human review is pending; the reviewer field says unreviewed." |
| 40–50 s | Run `bad.csv`. "One unitless height, `24`, is rejected with a plain error for row 2. The other nine still convert, and the exit code is non-zero." |
| 50–60 s | Open the GIS guide. "City GIS imports the GeoJSON or glTF and applies georeferencing. This tool makes no SLPK or I3S, and nothing has been uploaded." |

Don't describe the prisms as detailed models, and don't claim City approval, human review or a 90-building portfolio. Neither tower has podium storeys added.

## Commands

Run these after `mkdir -p output`:

```sh
csv2massing examples/csv2massing/synthetic/buildings.csv \
  --footprints examples/csv2massing/synthetic/footprints.geojson \
  --provenance examples/csv2massing/synthetic/provenance-manifest.json \
  --out output/demo-synthetic

csv2massing examples/csv2massing/surrey/buildings.csv \
  --footprints examples/csv2massing/surrey/footprints.geojson \
  --provenance examples/csv2massing/surrey/provenance-manifest.json \
  --out output/demo-surrey

csv2massing examples/csv2massing/synthetic/bad.csv \
  --footprints examples/csv2massing/synthetic/footprints.geojson \
  --provenance examples/csv2massing/synthetic/provenance-manifest.json \
  --out output/demo-bad; echo "exit code: $?"
```

Print the observed counts:

```sh
for d in output/demo-synthetic output/demo-surrey output/demo-bad; do
  python - "$d" <<'PY'
import json, sys
from pathlib import Path
_, target = sys.argv
out = Path(target)
report = json.loads((out / "validation.json").read_text())
print(out.name, report["ok"], report["reconciliation"])
PY
done
```

**Expected results:**
- **Synthetic:** `ok: true`, with 10 accepted rows, 10 buildings and 10 models.
- **Surrey:** `ok: true`, with 2 accepted rows, 2 buildings and 2 models.
- **Bad:** exit code 1 and `ok: false`, with 9 accepted rows, 1 rejected row, 9 buildings and 9 models. The terminal shows an `unitless_height` error for row 2.

If the observed results differ, say so and stop.

In the Surrey output, open `provenance/surrey-2354.json` and `provenance/surrey-132062.json`:
- **Wave:** 28 storeys from PLR_7915-0375-00, page 3.
- **Ultra:** 35 storeys from PLR_7917-0011-00, page 5. Page 21 of that report is the master-plan identity trace.
- **Wave discrepancy:** OSM lists 27 levels plus 2 roof levels. That difference is disclosed in provenance, not reconciled.

## GIS handoff

Open [arcgis-online-conversion.md](arcgis-online-conversion.md); don't log into an account. The `provenance_file` and `model_file` pointers are resolved against the output root. glTF `extras` hold the EPSG:26910 origin and WGS84 anchor, but GIS software doesn't apply them automatically. The operator applies the offsets during Import 3D Objects. Base zero is local ground, not a surveyed elevation.
