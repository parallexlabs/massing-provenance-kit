# Two human-traceable Surrey buildings

Public evidence inspected 2026-10-06. These are schematic demonstration records with human review explicitly pending. Existing footprints were copied unchanged; no point, floor area or site outline became an invented footprint. Floor area is unknown and remains blank.

| Building | Height input | Planning PDF / one-based page | Official footprint | Schematic conversion |
| --- | --- | --- | --- | --- |
| Wave Tower 1, 13303 Central Avenue | 28 storeys | [PLR_7915-0375-00](https://www.surrey.ca/sites/default/files/planning-reports/PLR_7915-0375-00.pdf), page 3 | OBJECTID 2354 | 86.5 m |
| Ultra Tower, West Village Phase 3, 13325 102A Avenue | 35 storeys | [PLR_7917-0011-00](https://www.surrey.ca/sites/default/files/planning-reports/PLR_7917-0011-00.pdf), page 5 | OBJECTID 132062 | 107.5 m |

The first PDF describes the existing Wave building east across 133 Street as “28-storey Wave apartment building.” The second describes Phase 3 as an existing 35-storey apartment tower, separately from at-grade townhouses. No townhouse or podium storeys were added to the tower count.

To trace either identity, open [PLR_7917-0011-00](https://www.surrey.ca/sites/default/files/planning-reports/PLR_7917-0011-00.pdf), PDF page 21 (Appendix II master plan). It names WAVE TOWER 1 on the west side of the block north of Central Avenue, and ULTRA TOWER / 35 Storeys in built Site C south of Central Avenue, north of 102 Avenue. Match these positions and outline shapes to the supplied GeoJSON. This uses a published plan as identity evidence, without extracting drawing geometry.

The actual polygon coordinates come from the [Surrey Building Footprints FeatureServer](https://services5.arcgis.com/YRpe0VKTJytZSSIB/arcgis/rest/services/Building%20Footprnts/FeatureServer/0). Both records have raw STATUS In Service. Their raw BUILDING_HEIGHT fields were not used because the field's measurement/unit provenance has not been established.

An additional cross-check uses [OSM way 1206021168](https://www.openstreetmap.org/way/1206021168) for Wave and [OSM way 390951278](https://www.openstreetmap.org/way/390951278) for Ultra. Projected intersection ratios against the official polygons were respectively 96.02% / 93.34% and 93.15% / 89.40% (intersection / OSM and intersection / official polygon). This is corroboration, not a replacement for checking the planning map. Wave's OSM record reports 27 levels plus 2 roof levels; the selected planning report says 28 storeys. The discrepancy remains in provenance.

The official outlines may include low attached portions. Uniform-height prisms omit setbacks, varied podium heights, rooftop equipment and architectural detail. The computed metres are schematic estimates from storeys, never measured heights. Neither building is a City-approved model. Reviewer fields say unreviewed; no human approval is implied.

Input files are under examples/csv2massing/surrey. Provenance contains full PDF hashes, page numbers, identity links, footprint method and uncertainty. Capture bytes and page renderings remain in the local excluded _build/live evidence directory; public source URLs and hashes allow independent checking.

The checked two-building output bundle is included at [surrey/result](../examples/csv2massing/surrey/result). It is byte-identical to the passing editable and installed-wheel runs. Wave has an [individual provenance record](../examples/csv2massing/surrey/result/provenance/surrey-2354.json) and [schematic glTF](../examples/csv2massing/surrey/result/models/surrey-2354.gltf); Ultra has an [individual provenance record](../examples/csv2massing/surrey/result/provenance/surrey-132062.json) and [schematic glTF](../examples/csv2massing/surrey/result/models/surrey-132062.gltf). Their reviewer fields remain explicitly unreviewed. The bundle includes the original-geometry GeoJSON, round-trip CSV and validation reports. Regenerate into a separate empty directory, because the CLI refuses an existing nonempty bundle. No GIS conversion or publication is implied.
