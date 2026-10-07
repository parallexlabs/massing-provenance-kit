"""Offline synthetic FeatureServer responses; no live records or contact data."""

import hashlib
import json
from contextlib import ExitStack
from datetime import UTC, datetime

import httpx
import pytest

import massing.public_sources as sources
from massing.heights import HeightSource

BBOX = (-122.85, 49.19, -122.84, 49.20)
MOMENT = datetime(2000, 1, 1, tzinfo=UTC)  # Fixed synthetic retrieval clock.
GEOMETRY = {"type": "Polygon", "coordinates": [[
    [-122.85, 49.19], [-122.8497, 49.19], [-122.8497, 49.1902],
    [-122.85, 49.1902], [-122.85, 49.19],
]]}
FOOTPRINT_FIELDS = ("OBJECTID", "FACILITY_TYPE", "BUILDING_HEIGHT", "STATUS")
APPLICATION_FIELDS = ("OBJECTID", "PROJECT_NO", "DESCRIPTION", "STATUS", "WEBLINK")


@pytest.fixture
def server():
    with ExitStack() as stack:
        def make(source=sources.FOOTPRINTS, ids=(3, 1, 2), edits=None):
            calls = []
            fields = APPLICATION_FIELDS if source == sources.APPLICATIONS else FOOTPRINT_FIELDS

            def transport(request):
                assert request.method == "GET"
                assert str(request.url).split("?")[0] in (source.url, source.url + "/query")
                params = dict(request.url.params)
                assert request.extensions["timeout"]["read"] > 0
                if request.url.path.endswith("/query"):
                    if params.get("returnIdsOnly") == "true":
                        stage, payload = "ids", {"objectIds": list(ids)}
                    elif params.get("returnCountOnly") == "true":
                        stage, payload = "count", {"count": len(ids)}
                    else:
                        stage = "page"
                        wanted = [int(value) for value in params["objectIds"].split(",")]
                        features = []
                        for oid in reversed(wanted):
                            attrs = {
                                "OBJECTID": oid, "FACILITY_TYPE": None, "BUILDING_HEIGHT": 99,
                                "STATUS": "SYNTHETIC raw status", "PROJECT_NO": "test-only://p",
                                "DESCRIPTION": "Synthetic 7-storey building.",
                                "WEBLINK": "test-only://application",
                                "APPLICANT": "PRIVATE SENTINEL", "CONTACT_EMAIL": "PRIVATE",
                            }
                            features.append({"type": "Feature", "id": oid,
                                             "geometry": GEOMETRY, "properties": attrs})
                        payload = {"type": "FeatureCollection", "features": features}
                else:
                    stage = "metadata"
                    payload = {"name": "SYNTHETIC layer", "maxRecordCount": 2,
                               "objectIdField": "OBJECTID",
                               "fields": [{"name": f} for f in (*fields, "CONTACT_EMAIL")],
                               "editingInfo": {"lastEditDate": 123, "ignored": True},
                               "copyrightText": "SYNTHETIC service licence text"}
                if edits and stage in edits:
                    edit = edits[stage]
                    payload = edit(payload) if callable(edit) else edit
                if isinstance(payload, Exception):
                    raise payload
                response = payload if isinstance(payload, httpx.Response) else (
                    httpx.Response(200, content=payload) if isinstance(payload, bytes)
                    else httpx.Response(200, json=payload)
                )
                calls.append((request, response.content))
                return response

            client = stack.enter_context(httpx.Client(transport=httpx.MockTransport(transport)))
            return client, calls
        yield make


def fetch(server, source=sources.FOOTPRINTS, ids=(3, 1, 2), edits=None, **limits):
    client, calls = server(source, ids, edits)
    snapshot = sources.fetch_layer(source, BBOX, client=client, now=lambda: MOMENT, **limits)
    return snapshot, calls


@pytest.mark.parametrize("bbox", [
    None, "bbox", (), (1, 2, 3), (True, 49.19, -122.84, 49.2),
    (-122.85, float("nan"), -122.84, 49.2), (-122.85, 49.19, float("inf"), 49.2),
    (-122.84, 49.19, -122.85, 49.2), (-122.85, 49.2, -122.84, 49.19),
    (-122.85, 49.19, -122.75, 49.2), (0, 49.19, .01, 49.2),
])
def test_invalid_bbox_rejected_before_request(server, bbox):
    client, calls = server()
    with pytest.raises(sources.SourceError):
        sources.fetch_layer(sources.FOOTPRINTS, bbox, client=client, now=lambda: MOMENT)
    assert not calls


@pytest.mark.parametrize("limits", [
    {"max_features": 0}, {"max_features": True}, {"max_features": 501},
    {"page_size": 0}, {"page_size": 101}, {"page_size": 1.5},
])
def test_invalid_limits_rejected_before_request(server, limits):
    client, calls = server()
    with pytest.raises(sources.SourceError):
        sources.fetch_layer(sources.FOOTPRINTS, BBOX, client=client, **limits)
    assert not calls


@pytest.mark.parametrize("source,fields", [
    (sources.FOOTPRINTS, FOOTPRINT_FIELDS), (sources.APPLICATIONS, APPLICATION_FIELDS),
])
def test_sorted_bounded_pages_provenance_and_manifest(server, source, fields):
    snapshot, calls = fetch(server, source, ids=(8, 3, 1, 2), max_features=3, page_size=100)
    assert snapshot.selected_ids == (1, 2, 3)
    assert [f["id"] for f in snapshot.features] == [1, 2, 3]
    assert snapshot.total_bbox_count == 4 and snapshot.truncated
    assert snapshot.max_record_count == 2 and snapshot.editing_info == {"lastEditDate": 123}
    assert snapshot.service_name == "SYNTHETIC layer"
    assert snapshot.licence_info == "SYNTHETIC service licence text"
    pages = [(request, body) for request, body in calls if "objectIds" in request.url.params]
    assert [dict(request.url.params)["objectIds"] for request, _ in pages] == ["1,2", "3"]
    for request, _ in pages:
        params = dict(request.url.params)
        assert params["outFields"].split(",") == list(fields)
        assert params["outSR"] == "4326" and params["returnGeometry"] == "true"
        assert "*" not in params["outFields"] and "CONTACT" not in params["outFields"]
    for request, _ in calls[1:3]:
        params = dict(request.url.params)
        assert params["inSR"] == "4326" and params["spatialRel"] == "esriSpatialRelIntersects"
        assert tuple(map(float, params["geometry"].split(","))) == BBOX
    assert len(snapshot.requests) == len(calls) == 5
    for record, (request, body) in zip(snapshot.requests, calls, strict=True):
        assert record.retrieved_at == MOMENT.isoformat() and record.status == 200
        assert record.sha256 == hashlib.sha256(body).hexdigest() and record.bytes == len(body)
        assert record.params == tuple(sorted(dict(request.url.params).items()))
        assert record.to_dict()["method"] == "GET"
    for feature in snapshot.features:
        props = feature["properties"]
        assert set(fields) <= set(props) and "APPLICANT" not in props
        assert "CONTACT_EMAIL" not in props and "PRIVATE" not in json.dumps(props)
        assert props["retrieved_at"] == MOMENT.isoformat() and props["source_url"] == source.url
        assert props["licence"] == source.licence and props["source_dataset"] == source.dataset
        assert props["source_sha256"] in {hashlib.sha256(body).hexdigest() for _, body in pages}
    manifest = snapshot.manifest()
    encoded = json.dumps(list(snapshot.features), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode()
    assert manifest["features_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert (manifest["selected_count"], manifest["total_bbox_count"], manifest["truncated"]) == (
        3, 4, True,
    )
    assert "ascending" in manifest["selection"] and "not an inventory" in manifest["selection"]
    assert manifest["requests"] == [record.to_dict() for record in snapshot.requests]
    assert snapshot.feature_collection()["manifest"] == manifest
    assert "BUILDING_HEIGHT" in manifest["not_evidence_fields"]


@pytest.mark.parametrize("edits,ids", [
    ({"ids": {}}, ()), ({"ids": {"objectIds": [True]}}, (1,)),
    ({"ids": {"objectIds": [1, 1]}}, (1, 2)),
    ({"ids": {"objectIds": ["1"]}}, (1,)),
    ({"count": {"count": True}}, (1,)), ({"count": {"count": 1.0}}, (1,)),
    ({"count": {"count": 9}}, (1,)), ({"count": {}}, (1,)),
])
def test_ids_and_count_integrity(server, edits, ids):
    with pytest.raises(sources.SourceError) as caught:
        fetch(server, ids=ids, edits=edits)
    assert caught.value.code


def test_empty_selection_is_explicit_not_truncated(server):
    snapshot, calls = fetch(server, ids=())
    assert snapshot.selected_ids == snapshot.features == ()
    assert snapshot.total_bbox_count == 0 and not snapshot.truncated and len(calls) == 3


@pytest.mark.parametrize("edit,code", [
    (lambda p: {**p, "features": p["features"][:1]}, "missing_records"),
    (lambda p: {**p, "features": [p["features"][0]] * 2}, "duplicate_records"),
    (lambda p: {**p, "features": [
        {**f, "properties": {**f["properties"], "OBJECTID": 99}} for f in p["features"]
    ][:1]}, "unexpected_records"),
    (lambda p: {**p, "exceededTransferLimit": True}, "transfer_limit"),
    (lambda p: {**p, "properties": {"exceededTransferLimit": True}}, "transfer_limit"),
    (lambda p: {**p, "features": None}, "invalid_payload"),
    (lambda p: {**p, "features": [{"properties": {"OBJECTID": True}}]}, "invalid_record"),
])
def test_page_integrity(server, edit, code):
    with pytest.raises(sources.SourceError) as caught:
        fetch(server, ids=(1, 2), edits={"page": edit})
    assert caught.value.code == code


@pytest.mark.parametrize("edit,code", [
    (lambda p: {**p, "maxRecordCount": True}, "invalid_metadata"),
    (lambda p: {**p, "fields": []}, "missing_fields"),
    (lambda p: {**p, "objectIdField": "WRONG"}, "id_field_mismatch"),
])
def test_metadata_failures(server, edit, code):
    with pytest.raises(sources.SourceError) as caught:
        fetch(server, edits={"metadata": edit})
    assert caught.value.code == code


@pytest.mark.parametrize("stage", ["metadata", "ids", "count", "page"])
@pytest.mark.parametrize("payload,code", [
    (httpx.Response(503, text="synthetic failure"), "http_status"),
    (httpx.ReadTimeout("synthetic timeout"), "http_error"),
    ({"error": {"code": 400, "message": "synthetic service failure"}}, "service_error"),
    (b"not JSON", "invalid_json"), (b"[]", "invalid_json"), (b"\xff", "invalid_json"),
])
def test_explicit_request_service_and_json_failures(server, stage, payload, code):
    with pytest.raises(sources.SourceError) as caught:
        fetch(server, edits={stage: payload})
    assert caught.value.code == code


@pytest.mark.parametrize("text,count", [
    ("Synthetic 7-storey building", 7), ("10 storeys", 10), ("4 stories", 4),
    ("1 story", 1), ("61 STOREYS", 61), ("1234 storeys", 1234),
])
def test_explicit_integer_storeys(text, count):
    assert sources.extract_storeys(text) == (count, "explicit_storeys")


@pytest.mark.parametrize("text", [
    "3.7 storeys", "-3 storeys", "3-7 storeys", "3 to 7 storeys",
    "3\u20137 storeys", "7 storeys and a single-storey wing", "3 storeys and 7 storeys",
    "7 storeys; another 7-storey wing", "0 storeys", "No explicit count", None,
])
def test_unsafe_or_ambiguous_storeys_never_become_evidence(text):
    count, status = sources.extract_storeys(text)
    assert count is None and status != "explicit_storeys"


@pytest.mark.parametrize("weblink", ["test-only://application", None])
def test_application_real_field_raw_status_reference_and_date(server, weblink):
    def edit(page):
        for feature in page["features"]:
            feature["properties"]["WEBLINK"] = weblink
        return page
    snapshot, _ = fetch(server, sources.APPLICATIONS, ids=(1,), edits={"page": edit})
    record, = sources.application_records(snapshot.features)
    assert record.site_id == "1" and record.project_ref == "test-only://p"
    assert record.status == "SYNTHETIC raw status"
    assert (record.description, record.storeys, record.storeys_status) == (
        "Synthetic 7-storey building.", 7, "explicit_storeys")
    assert record.reference == (weblink or "test-only://p")
    assert record.retrieved_at == MOMENT.isoformat()


def test_raw_building_height_is_not_ladder_evidence(server, decide):
    snapshot, _ = fetch(server, ids=(1,))
    feature, = snapshot.features
    assert feature["properties"]["BUILDING_HEIGHT"] == 99
    assert not {"height_m", "height_source", "height_confidence"} & feature["properties"].keys()
    assert sources.footprint_items(snapshot.features) == [("1", feature["geometry"])]
    decision = decide([], building_id="1")
    assert decision.source is HeightSource.UNKNOWN and decision.height_m == 4
