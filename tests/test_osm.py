"""Offline OSM fixtures and MockTransport responses, never actual live retrievals."""

import hashlib
import json
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from shapely.affinity import scale, translate

import massing.osm as osm
from massing.heights import HeightSource
from massing.public_sources import SourceError
from massing.simplify import prepare_footprints

BBOX = (-122.85, 49.19, -122.84, 49.20)
MOMENT = datetime(2000, 1, 1, tzinfo=UTC)
MOCK_PAYLOAD = {"generator": "SYNTHETIC MockTransport real-format response", "elements": [],
                "osm3s": {"timestamp_osm_base": MOMENT.isoformat()}}


@pytest.fixture
def api():
    with ExitStack() as stack:
        def make(reply=MOCK_PAYLOAD):
            calls = []
            def transport(request):
                assert request.method == "GET"
                assert str(request.url).split("?")[0] == osm.OVERPASS_URL
                assert request.extensions["timeout"]["read"] > 0
                calls.append(request)
                if isinstance(reply, Exception):
                    raise reply
                if isinstance(reply, httpx.Response):
                    return reply
                return httpx.Response(200, content=reply) if isinstance(reply, bytes) else httpx.Response(200, json=reply)
            client = stack.enter_context(httpx.Client(transport=httpx.MockTransport(transport)))
            return client, calls
        yield make


def fixture_payload(sample):
    body = json.dumps(sample, sort_keys=True).encode()
    payload, provenance = osm.load_osm_payload(body, retrieved_at=None)
    assert provenance.payload_sha256 == hashlib.sha256(body).hexdigest()
    return payload, provenance


def test_query_is_bounded_and_wgs84_ordered():
    query = osm.overpass_query(BBOX)
    timeout_clause = "[" + "timeout" + ":" + "25" + "]"
    assert timeout_clause in query and 'out tags geom 5001;' in query
    assert '49.1900000,-122.8500000,49.2000000,-122.8400000' in query
    assert 'way["building"]["height"]' in query and 'way["building"]["building:levels"]' in query
    with pytest.raises(SourceError):
        osm.overpass_query((0, 0, 1, 1))


def test_cache_dates_query_hash_payload_hash_and_explicit_refresh(api, tmp_path):
    client, calls = api()
    payload, first = osm.fetch_overpass(BBOX, client=client, cache_dir=tmp_path,
                                        now=lambda: MOMENT)
    assert (first.retrieved_at, first.osm_base, first.licence, first.source_url) == (
        MOMENT.isoformat(), MOMENT.isoformat(), osm.OSM_LICENCE, osm.OVERPASS_URL)
    assert dict(calls[0].url.params) == {"data": osm.overpass_query(BBOX)}
    path, = tmp_path.glob("*.json")
    record = json.loads(path.read_text())
    assert record["query"] == first.query and record["query_sha256"] == hashlib.sha256(first.query.encode()).hexdigest()
    assert record["payload_sha256"] == hashlib.sha256(record["payload_text"].encode()).hexdigest()
    assert record["retrieved_at"] == first.retrieved_at
    _, cached = osm.fetch_overpass(BBOX, client=client, cache_dir=tmp_path,
                                   now=lambda: MOMENT + timedelta(days=1))
    assert cached.from_cache and cached.retrieved_at == first.retrieved_at and len(calls) == 1
    assert cached.payload_sha256 == first.payload_sha256
    _, refreshed = osm.fetch_overpass(BBOX, client=client, cache_dir=tmp_path, refresh=True,
                                      now=lambda: MOMENT + timedelta(days=2))
    assert not refreshed.from_cache and len(calls) == 2
    assert refreshed.retrieved_at == (MOMENT + timedelta(days=2)).isoformat() == json.loads(path.read_text())["retrieved_at"]


@pytest.mark.parametrize("field,value", [
    ("query", "different query"), ("query_sha256", "bad"), ("payload_sha256", "bad"),
    ("payload_text", "{}"), ("retrieved_at", ""), ("extra", True),
])
def test_cache_tamper_never_silently_refetches(api, tmp_path, field, value):
    client, calls = api()
    osm.fetch_overpass(BBOX, client=client, cache_dir=tmp_path, now=lambda: MOMENT)
    path, = tmp_path.glob("*.json")
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(SourceError) as caught:
        osm.fetch_overpass(BBOX, client=client, cache_dir=tmp_path, now=lambda: MOMENT)
    assert caught.value.code == "cache_integrity" and len(calls) == 1
    osm.fetch_overpass(BBOX, client=client, cache_dir=tmp_path, refresh=True, now=lambda: MOMENT)
    assert len(calls) == 2


def test_unreadable_cache_and_changed_bbox(api, tmp_path):
    with pytest.raises(SourceError) as caught:
        osm.read_cache(tmp_path / "absent.json", osm.overpass_query(BBOX))
    assert caught.value.code == "cache_unreadable"
    client, calls = api()
    osm.fetch_overpass(BBOX, client=client, cache_dir=tmp_path, now=lambda: MOMENT)
    osm.fetch_overpass((-122.849, 49.19, -122.84, 49.20), client=client,
                       cache_dir=tmp_path, now=lambda: MOMENT)
    assert len(calls) == 2 and len(list(tmp_path.glob("*.json"))) == 2


@pytest.mark.parametrize("reply,code", [
    (httpx.Response(429), "http_status"), (httpx.ReadTimeout("synthetic"), "http_error"),
    (b"broken JSON", "invalid_json"), ({"elements": None}, "invalid_payload"),
    ({"error": {"message": "synthetic failure"}}, "service_error"),
    ({"elements": [], "remark": "Query timed out"}, "overpass_error"),
])
def test_fetch_failure_never_writes_cache(api, tmp_path, reply, code):
    client, _ = api(reply)
    with pytest.raises(SourceError) as caught:
        osm.fetch_overpass(BBOX, client=client, cache_dir=tmp_path, now=lambda: MOMENT)
    assert caught.value.code == code and not list(tmp_path.iterdir())


@pytest.mark.parametrize("stamp", [None, ""])
def test_real_format_data_requires_retrieval_date(stamp):
    with pytest.raises(SourceError) as caught:
        osm.load_osm_payload(json.dumps(MOCK_PAYLOAD).encode(), retrieved_at=stamp)
    assert caught.value.code == "missing_provenance"


def test_synthetic_fixture_provenance_and_live_rejection(osm_sample, api):
    _, provenance = fixture_payload(osm_sample)
    assert provenance.test_only and provenance.source_url == "test-only://osm"
    assert (provenance.retrieved_at, provenance.licence) == (
        "not-applicable-synthetic", osm_sample["fixture_provenance"]["licence"],
    )
    client, _ = api(osm_sample)
    with pytest.raises(SourceError) as caught:
        osm.fetch_overpass(BBOX, client=client, now=lambda: MOMENT)
    assert caught.value.code == "invalid_payload"


@pytest.mark.parametrize("kind,code", [
    ("nonobject", "malformed_element"), ("node", "unsupported_element"),
    ("relation", "unsupported_element"), ("bool_id", "unsupported_element"),
    ("missing", "missing_geometry"), ("nonfinite", "missing_geometry"),
    ("unclosed", "unclosed_way"), ("invalid", "invalid_geometry"),
])
def test_rejected_native_elements_are_reported(osm_sample, kind, code):
    way = deepcopy(osm_sample["elements"][0])
    if kind == "nonobject":
        way = None
    elif kind in {"node", "relation"}:
        way["type"] = kind
    elif kind == "bool_id":
        way["id"] = True
    elif kind == "missing":
        del way["geometry"]
    elif kind == "nonfinite":
        way["geometry"][0]["lon"] = float("inf")
    elif kind == "unclosed":
        way["geometry"].pop()
    else:
        ring = way["geometry"]
        way["geometry"] = [ring[0], ring[2], ring[1], ring[3], ring[0]]
    ways, findings = osm.parse_ways({"elements": [way]})
    assert not ways and [f.code for f in findings] == [code] and findings[0].detail


def test_duplicate_ways_and_truncation_are_not_silently_used(osm_sample):
    way = osm_sample["elements"][0]
    ways, findings = osm.parse_ways({"elements": [way, deepcopy(way)]})
    assert not ways and [f.code for f in findings] == ["duplicate_element"] * 2
    with pytest.raises(SourceError) as caught:
        osm.parse_ways({"elements": [None] * 5001})
    assert caught.value.code == "overpass_truncated"


def test_strong_join_preserves_evidence_floor_provenance_and_ladder(osm_sample, footprints_sample, decide):
    sample = deepcopy(osm_sample)
    sample["elements"][0]["tags"].update({"min_height": "2 m", "name": "discard me"})
    payload, provenance = fixture_payload(sample)
    ways, _ = osm.parse_ways(payload)
    assert all("name" not in way.tags for way in ways)
    batch = prepare_footprints([(f["id"], f["geometry"]) for f in footprints_sample["features"]])
    result = osm.join_osm(payload, provenance, {f.building_id: f.geom_utm for f in batch.footprints})
    assert (result.element_count, result.usable_way_count) == (2, 2)
    assert set(result.evidence) == {"b1", "b2"} and not result.findings
    assert result.unmatched_footprint_ids == ("b3", "b4", "b5")
    evidence = result.evidence["b1"]
    assert evidence.test_only and evidence.licence == provenance.licence
    assert evidence.retrieved_at == provenance.retrieved_at and evidence.min_height == "2 m"
    assert evidence.min_height_reference.startswith("test-only:")
    assert [(c.source, c.value) for c in evidence.candidates] == [
        (HeightSource.OSM_HEIGHT, "24 m"), (HeightSource.OSM_LEVELS, "10"),
    ]
    for candidate in evidence.candidates:
        assert candidate.reference == evidence.min_height_reference
        assert candidate.reference.endswith(evidence.osm_id)
        assert candidate.source_text and candidate.retrieved_at == provenance.retrieved_at
    decision = decide(evidence.candidates, min_height=evidence.min_height)
    assert (decision.height_m, decision.source, decision.min_height_m) == (24, HeightSource.OSM_HEIGHT, 2)
    assert decision.alternatives[0].height_m == 32.5
    assert decide(result.evidence["b2"].candidates).height_m == 32.5


@pytest.mark.parametrize("factor,accepted", [(.5, False), (.9, True), (1.1, True), (2, False)])
def test_overlap_requires_eighty_percent_of_both_areas(osm_sample, factor, accepted):
    sample = deepcopy(osm_sample)
    sample["elements"] = sample["elements"][:1]
    payload, provenance = fixture_payload(sample)
    ways, _ = osm.parse_ways(payload)
    footprint = scale(ways[0].geom_utm, xfact=factor, yfact=factor, origin="centroid")
    result = osm.join_osm(payload, provenance, {"b1": footprint})
    assert bool(result.evidence) is accepted
    if accepted:
        assert result.matches[0].footprint_overlap >= .8 and result.matches[0].way_overlap >= .8
    else:
        assert [f.code for f in result.findings] == ["weak_overlap_only"]


def test_one_to_one_ambiguity_conflicts_and_no_match(osm_sample):
    sample = deepcopy(osm_sample)
    sample["elements"] = sample["elements"][:1]
    payload, provenance = fixture_payload(sample)
    ways, _ = osm.parse_ways(payload)
    geometry = ways[0].geom_utm
    result = osm.join_osm(payload, provenance, {"b1": geometry, "b2": geometry})
    assert not result.evidence and result.findings[0].code == "ambiguous_osm_way"
    other = deepcopy(sample["elements"][0])
    other["id"] += 1
    sample["elements"].append(other)
    payload, provenance = fixture_payload(sample)
    result = osm.join_osm(payload, provenance, {"b1": geometry})
    assert not result.evidence and result.findings[0].code == "conflicting_osm_ways"
    assert len(result.findings[0].candidate_ids) == 2
    result = osm.join_osm(payload, provenance, {"far": translate(geometry, xoff=1000)})
    assert not result.matches and result.unmatched_footprint_ids == ("far",)
    assert {f.code for f in result.findings} == {"unmatched_osm_way"}
