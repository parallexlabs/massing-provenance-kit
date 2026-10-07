# City GIS conversion and ArcGIS Online loading

This is a documented operator workflow, checked against official Esri documentation on 2026-10-06. No ArcGIS Online upload or publishing action was executed here. csv2massing does not create SLPK or I3S.

## GeoJSON to a schematic web scene

1. Run csv2massing and read validation.json. Resolve rejected rows. Exclude null height_m features from the extrusion layer; retain those records separately as unknown, with their provenance.
2. In the City's ArcGIS Online account, add massing.geojson as a GeoJSON item and publish a hosted feature layer where organizational permissions allow. Keep id, height_m, height_unit, height_method, schematic, csv_source_pdf, evidence_page and provenance_file fields. JSON paths are local package pointers: host the per-building provenance records at an approved City location and populate a provenance URL field before sharing a scene.
3. Add the polygon layer to Scene Viewer. Select no main attribute in the style gallery, then 3D Extrusion. For Height select height_m under Attributes and choose metres. Use the layer's ground-relative placement for a schematic visualization; base_height_m=0 is local building ground, not surveyed absolute elevation. Filter out null heights rather than assigning a default. Configure pop-ups with source PDF, page and the hosted provenance URL.
4. Inspect both known-height buildings against the basemap, compare dimensions with the validation/provenance records, label them schematic and save the web scene under City controls.

Esri documents [GeoJSON publishing as hosted feature layers](https://doc.arcgis.com/en/arcgis-online/manage-data/publish-features.htm), [adding GeoJSON and feature layers to scenes](https://doc.arcgis.com/en/arcgis-online/create-maps/add-layers-to-scene.htm), and [3D Extrusion using an attribute with an explicit measurement unit](https://doc.arcgis.com/en/arcgis-online/create-maps/scene-style-polygons.htm). Organizational privileges and the current interface must be checked by the City operator.

## Per-building glTF to ArcGIS Pro

The .gltf files contain local east/up/negative-north vertices in metres, with a deterministic projected origin and WGS84 anchor in extras. Extras are metadata: ArcGIS is not assumed to automatically apply their georeferencing.

In ArcGIS Pro, use Import 3D Objects, which explicitly accepts .gltf. Select an existing 3D object feature class explicitly defined in NAD83 / UTM zone 10N, EPSG:26910, when applying the recorded offsets. Import each model separately with scale 1, its recorded easting/northing XY offset and the appropriate surveyed elevation offset if available. Confirm that the format importer interprets Y-up as vertical and that north/east and elevation agree with massing.geojson. Do not accept a model at coordinate origin or use a guessed absolute elevation. If another output CRS is required, reproject the georeferenced features afterward; do not apply EPSG:26910 offsets directly in another CRS.

Join the building id and provenance attributes to the imported 3D objects. The City GIS operator can then use the organization's approved ArcGIS Pro web-scene / scene-layer publishing workflow to ArcGIS Online, resolving its analyzer and licensing requirements. That publishing/conversion step belongs to City GIS and is outside this CLI.

The official [Import 3D Objects documentation](https://pro.arcgis.com/en/pro-app/latest/tool-reference/data-management/import-3d-objects.htm) lists .gltf and XY/elevation/scale offsets; it also warns that models without a spatial reference otherwise default to WKID 3857. The [3D models workflow](https://doc.arcgis.com/en/3d/workflows/content/3d-models.htm) covers importing and placing models. Do not confuse Import 3D Objects with Import 3D Files, whose supported input formats differ. A glTF file alone is not claimed to be an ArcGIS Online scene item.
