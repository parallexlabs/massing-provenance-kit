"""Archived-v1 QA regressions [25]; not CSV-v2 fallback tests. Results unknown."""

import math
from dataclasses import replace

import pytest

from massing.extrude import extrude
from massing.heights import EvidenceCandidate, EvidenceIssue, HeightSource
from massing.match import MatchFinding, MatchResult
from massing.osm import OsmFinding, OsmJoinResult, OsmMatch, OsmProvenance
from massing.qa import (
    ExtrusionFailure,
    QAInputs,
    RunInfo,
    SourceManifest,
    StoreyCandidate,
    build_qa,
    render_markdown,
)
from massing.simplify import Exclusion, GeometryChange, prepare_footprints

REF = "test-only://not-city-evidence"


@pytest.fixture
def qa_inputs(footprints_sample, decide):
    prepared = prepare_footprints([(f["id"], f["geometry"])
                                   for f in footprints_sample["features"]])
    assert prepared.reconciled and len(prepared.footprints) == 5
    prepared = replace(prepared, footprints=tuple(replace(f, width_m=10) for f in prepared.footprints))
    run = RunInfo("synthetic-tool", "synthetic-python", "offline", dependency_versions=(
        ("z-dependency", "test-2"), ("a-dependency", "test-1")), config_hashes=(
        ("z-config.yaml", "b" * 64), ("a-config.yaml", "a" * 64)))
    sources = tuple(SourceManifest(role, f"synthetic-{role}", f"test-only://{role}",
                                   f"synthetic-{role}-licence", "not-applicable-synthetic",
                                   "c" * 64, 5, 9, True, True,
                                   "test-only://licence", "synthetic query")
                    for role in ("overrides", "applications", "height_evidence", "footprints"))
    decisions = {f.building_id: decide(building_id=f.building_id) for f in prepared.footprints}
    return QAInputs(run, sources, 5, prepared, decisions, tuple(sorted(decisions)))


def with_decisions(inputs, decisions):
    return replace(inputs, decisions=decisions, output_ids=tuple(sorted(decisions)))


def test_empty_report_is_reconciled_with_no_hidden_evidence(qa_inputs):
    empty = prepare_footprints([])
    report = build_qa(replace(qa_inputs, prepared=empty, input_footprint_count=0,
                              decisions={}, output_ids=(), sources=()))
    assert report.reconciliation.reconciled and report.reconciliation.output_count == 0
    assert sum(dict(report.source_counts).values()) == 0
    assert not report.findings and not report.storey_rows and not report.ratio_outliers
    text = report.to_markdown()
    assert "n/a" in text and "_None._" in text and text.endswith("\n")
    assert "ATTENTION" not in text


@pytest.mark.parametrize("field,value", [
    ("input_footprint_count", -1), ("input_footprint_count", True),
    ("input_footprint_count", 1.5), ("override_rows_total", -1),
    ("override_rows_total", False), ("override_rows_total", 1.5),
    ("ratio_threshold", 0), ("ratio_threshold", -1), ("ratio_threshold", True),
    ("ratio_threshold", float("nan")), ("ratio_threshold", float("inf")),
])
def test_invalid_qa_inputs_rejected(qa_inputs, field, value):
    with pytest.raises(ValueError):
        build_qa(replace(qa_inputs, **{field: value}))


@pytest.mark.parametrize("field,value", [
    ("mode", "unrecognized"), ("tool_version", ""), ("python_version", "two\nlines"),
    ("command", "two\rlines"), ("command", None),
])
def test_invalid_run_metadata_rejected(qa_inputs, field, value):
    with pytest.raises(ValueError):
        replace(qa_inputs.run, **{field: value})


@pytest.mark.parametrize("field,value", [
    ("role", "unrecognized"), ("name", " "), ("url", "two\nlines"),
    ("licence", "two\rlines"), ("licence", None),
])
def test_invalid_source_manifest_rejected(qa_inputs, field, value):
    with pytest.raises(ValueError):
        replace(qa_inputs.sources[0], **{field: value})


def test_reconciliation_exclusions_failures_and_geometry_findings(qa_inputs):
    change = GeometryChange("b1", "repaired", "synthetic repair was reported")
    batch = replace(qa_inputs.prepared, input_count=6,
                    exclusions=(Exclusion("dropped", "sliver", "synthetic area below 10 m2"),),
                    changes=(change, replace(change, building_id="b2")))
    decisions = {key: value for key, value in qa_inputs.decisions.items() if key != "b5"}
    footprint = batch.footprints[0]
    mesh = extrude("b1", footprint.geom_utm, 0, 4, (0, 0))
    inputs = replace(with_decisions(qa_inputs, decisions), prepared=batch, input_footprint_count=6,
                     extrusion_failures=(ExtrusionFailure("b5", "synthetic triangulation failure"),),
                     meshes=(replace(mesh, cap_triangles=mesh.expected_cap_triangles - 1),))
    report = build_qa(inputs)
    r = report.reconciliation
    assert (r.input_count, r.prepared_count, r.geometry_excluded, r.extrusion_failed,
            r.output_count, r.reconciled) == (6, 5, 1, 1, 4, True)
    assert not r.discrepancies
    assert set(report.finding_counts) == {
        ("geometry", "repaired", 2), ("geometry_exclusion", "sliver", 1),
        ("extrusion", "extrusion_failed", 1), ("extrusion", "cap_triangle_count", 1),
    }
    assert all(f.detail for f in report.findings)
    assert "synthetic triangulation failure" in report.to_markdown()
    reordered = replace(inputs, prepared=replace(batch, changes=tuple(reversed(batch.changes))))
    assert build_qa(reordered).to_markdown() == report.to_markdown()


@pytest.mark.parametrize("case,phrase", [
    ("input", "input"), ("prepared", "geometry stage"), ("duplicate", "duplicate output"),
    ("failure", "unknown ids"), ("output", "missing"), ("decisions", "height decisions"),
])
def test_reconciliation_reports_mismatches_instead_of_hiding_them(qa_inputs, case, phrase):
    inputs = qa_inputs
    if case == "input":
        inputs = replace(inputs, input_footprint_count=6)
    elif case == "prepared":
        inputs = replace(inputs, prepared=replace(inputs.prepared, footprints=()))
    elif case == "duplicate":
        inputs = replace(inputs, output_ids=(*inputs.output_ids, "b1"))
    elif case == "failure":
        inputs = replace(inputs, extrusion_failures=(ExtrusionFailure("missing", "failed"),))
    elif case == "output":
        inputs = replace(inputs, output_ids=("b1", "b2", "b3", "b4", "unexpected"))
    else:
        inputs = replace(inputs, decisions={})
    report = build_qa(inputs)
    assert not report.reconciliation.reconciled
    assert any(phrase in item for item in report.reconciliation.discrepancies)
    assert "ATTENTION: counts do not reconcile" in report.to_markdown()


def test_ladder_counts_confidence_and_override_categories(qa_inputs, decide):
    base = decide([EvidenceCandidate(HeightSource.OSM_LEVELS, 10)], building_id="b1")
    test = decide([EvidenceCandidate(HeightSource.CITY_SUPPLIED, 40, reference=REF,
                                    test_only=True)], building_id="b2")
    # Real-category rendering only; this remains synthetic evidence, not City data.
    real = replace(test, building_id="b1", test_only=False)
    decisions = {"b1": real, "b2": test, "b3": replace(base, building_id="b3"),
                 "b4": decide(building_id="b4", use_class="office"),
                 "b5": decide(building_id="b5")}
    report = build_qa(with_decisions(qa_inputs, decisions))
    assert dict(report.source_counts) == {"city_supplied": 2, "osm_height": 0, "osm_levels": 1,
                                         "application_storeys": 0, "typology_default": 1, "unknown": 1}
    assert report.real_override_ids == ("b1",) and report.test_only_override_ids == ("b2",)
    assert report.typology_ids == ("b4",) and report.unknown_ids == ("b5",)
    text = report.to_markdown()
    assert "NOT City evidence" in text and "not a statistical probability" in text
    for source, confidence, count in [
        ("city_supplied", 1, 2), ("osm_height", .9, 0), ("osm_levels", .7, 1),
        ("application_storeys", .6, 0), ("typology_default", .3, 1), ("unknown", .1, 1),
    ]:
        assert f"| {source} | {confidence:.3f} | {count} |" in text
    assert "| osm_levels | 0.700 | 1 | 20.0% |" in text
    assert "Uncertain (typology_default + unknown, dashed outline): 2 of 5 (40.0%)" in text
    assert "Unknown (4 m placeholder, confidence 0.1)" in text


def test_issues_override_rows_and_feet_detection(qa_inputs, decide):
    decisions = dict(qa_inputs.decisions)
    decisions["b1"] = decide([EvidenceCandidate(HeightSource.OSM_HEIGHT, "10 feet",
                                               source_text="height=10 feet")])
    decisions["b2"] = decide([EvidenceCandidate(HeightSource.OSM_HEIGHT, "bad"),
                              EvidenceCandidate(HeightSource.APPLICATION_STOREYS, 10,
                                                reference="test-only://application")], building_id="b2")
    decisions["b3"] = decide([
        EvidenceCandidate(HeightSource.OSM_HEIGHT, "10 feet", source_text="height=10 feet"),
        EvidenceCandidate(HeightSource.CITY_SUPPLIED, 40, reference=REF, test_only=True),
    ], building_id="b3")
    issue = EvidenceIssue("unmatched", "city_supplied", "unmatched_override_id",
                          "No corresponding footprint", "40", 3)
    inputs = replace(with_decisions(qa_inputs, decisions), override_rows_total=2,
                     override_issues=(issue, replace(issue, building_id="duplicate",
                                                    code="duplicate_override_id", row=2)))
    report = build_qa(inputs)
    assert report.feet_ids == ("b1",) and report.override_rows_total == 2
    assert [i.building_id for i in report.override_issues] == ["duplicate", "unmatched"]
    assert report.evidence_issues == decisions["b2"].issues
    assert dict(report.source_counts)["osm_height"] == dict(report.source_counts)["application_storeys"] == 1
    text = report.to_markdown()
    assert "Rows read: 2" in text and "Rejected or unmatched: 2" in text
    assert "unmatched_override_id" in text and "malformed" in text


def test_matching_and_osm_findings_remain_visible(qa_inputs):
    match_findings = tuple(MatchFinding(
        "footprint" if code == "conflicting_project_matches" else "site",
        sid, code, "synthetic finding", candidates)
        for sid, code, candidates in [
            ("s1", "distance_tie", ("b1", "b2")),
            ("s2", "too_many_candidates", ("b1", "b2", "b3", "b4")),
            ("s3", "no_candidates", ()),
            ("b3", "conflicting_project_matches", ("s4", "s5")),
        ])
    match = MatchResult(5, 5, (), match_findings, ("s1", "s2", "s3"), ("b4", "b5"), ("b3",))
    provenance = OsmProvenance("test-only://osm", None, "not-applicable-synthetic",
                               "d" * 64, "synthetic-height-licence", test_only=True)
    osm_findings = (OsmFinding("osm_way", "way/1", "weak_overlap_only", "weak", ("b2",)),
                    OsmFinding("osm_way", "way/2", "unmatched_osm_way", "unmatched"))
    joined = OsmJoinResult(provenance, 8, 3, (OsmMatch("b1", "way/3", .9, .9),), {},
                           osm_findings, ("b2", "b3", "b4", "b5"))
    report = build_qa(replace(qa_inputs, match=match, osm=joined))
    assert report.unmatched_site_ids == match.unmatched_site_ids
    assert report.unmatched_footprint_ids == ("b4", "b5")
    assert report.conflicted_footprint_ids == ("b3",)
    assert dict(report.osm_summary) == {"elements": 8, "usable_closed_ways": 3,
                                       "accepted_matches": 1, "footprints_without_osm": 4, "findings": 2}
    assert len(report.findings) == 6
    text = report.to_markdown()
    for finding in (*match_findings, *osm_findings):
        assert finding.code in text and finding.subject_id in text
    assert "candidates" in text and "b4" in text


def test_storey_outliers_cover_all_evidence_without_clamping(qa_inputs, decide):
    decisions = dict(qa_inputs.decisions)
    decisions["b1"] = decide([EvidenceCandidate(HeightSource.OSM_LEVELS, 61)])
    decisions["b2"] = decide([EvidenceCandidate(HeightSource.OSM_HEIGHT, 24),
                              EvidenceCandidate(HeightSource.OSM_LEVELS, 62),
                              EvidenceCandidate(HeightSource.APPLICATION_STOREYS, 63, reference=REF)],
                             building_id="b2")
    decisions["b3"] = decide([EvidenceCandidate(HeightSource.OSM_LEVELS, 64),
                              EvidenceCandidate(HeightSource.CITY_SUPPLIED, 40, reference=REF,
                                                test_only=True)], building_id="b3")
    decisions["b5"] = decide([EvidenceCandidate(HeightSource.OSM_LEVELS, 60)], building_id="b5")
    candidates = (StoreyCandidate("attached", "b4", 65, "explicit", REF, False),
                  StoreyCandidate("unmatched", None, 66, "unmatched", REF, False),
                  StoreyCandidate("boundary", "b5", 60, "explicit", REF, True),
                  StoreyCandidate("unknown", "b4", None, "no_text", None, False))
    report = build_qa(replace(with_decisions(qa_inputs, decisions), application_candidates=candidates))
    assert {(o.subject_id, o.storeys, o.origin) for o in report.storey_outliers} == {
        ("b1", 61, "current"), ("b2", 62, "alternative"), ("b2", 63, "alternative"),
        ("b3", 64, "history"), ("site:attached", 65, "application_candidate"),
        ("site:unmatched", 66, "application_candidate"),
    }
    rows = {row.building_id: row for row in report.storey_rows}
    assert rows["b2"].alternatives == (62, 63) and rows["b3"].history == (64,)
    assert rows["b5"].current == 60 and len(rows["b4"].candidates) == 2
    assert decisions["b1"].height_m == 190.6 and decisions["b1"].storeys == 61
    assert "not corrected" in report.to_markdown() and "no_text" in report.to_markdown()


@pytest.mark.parametrize("height,width,threshold,flagged", [
    (80, 10, 8, False), (80.001, 10, 8, True), (80, 0, 8, True),
    (40, 10, 4, False), (40.01, 10, 4, True),
])
def test_height_width_ratio_strict_boundary(qa_inputs, decide, height, width, threshold, flagged):
    footprint = replace(qa_inputs.prepared.footprints[0], width_m=width)
    batch = replace(qa_inputs.prepared, footprints=(footprint, *qa_inputs.prepared.footprints[1:]))
    decisions = dict(qa_inputs.decisions)
    decisions["b1"] = decide([EvidenceCandidate(HeightSource.OSM_HEIGHT, height)])
    report = build_qa(replace(with_decisions(qa_inputs, decisions), prepared=batch, ratio_threshold=threshold))
    assert bool(report.ratio_outliers) is flagged and report.ratio_threshold == threshold
    if flagged:
        outlier, = report.ratio_outliers
        assert (outlier.building_id, outlier.height_m, outlier.width_m) == ("b1", height, width)
        assert outlier.ratio == (height / width if width else math.inf)
    assert decisions["b1"].height_m == height


def test_markdown_manifest_escaping_and_order_are_deterministic(qa_inputs):
    issue = EvidenceIssue("b1", "osm_height", "malformed", "a|b\\c\nd\re", "raw|value")
    decisions = dict(qa_inputs.decisions)
    decisions["b1"] = replace(decisions["b1"], issues=(issue,))
    source = replace(qa_inputs.sources[0], name=r"source|path\file", query="first\nsecond")
    inputs = replace(with_decisions(qa_inputs, decisions), sources=(source, *qa_inputs.sources[1:]))
    report = build_qa(inputs)
    text = report.to_markdown()
    assert text == render_markdown(report) == report.to_markdown()
    reordered = replace(inputs, sources=tuple(reversed(inputs.sources)),
                        decisions=dict(reversed(list(inputs.decisions.items()))),
                        run=replace(inputs.run, mode="offline",
                                    dependency_versions=tuple(reversed(inputs.run.dependency_versions)),
                                    config_hashes=tuple(reversed(inputs.run.config_hashes))))
    assert build_qa(reordered).to_markdown() == text
    assert r"source\|path\\file" in text and r"a\|b\\c d e" in text
    assert "first second" in text and "first\nsecond" not in text
    for source in inputs.sources:
        assert source.licence in text and source.url in text
    assert "not-applicable-synthetic" in text and "c" * 64 in text
    assert "a" * 64 in text and "b" * 64 in text and "synthetic-python" in text
    assert "Footprint geometry sources (licence covers geometry only)" in text
    assert "Height evidence sources (licence covers height evidence only)" in text
    assert "| 5 | 9 | yes | yes |" in text
    assert text.index("a-dependency") < text.index("z-dependency")
    assert text.index("a-config.yaml") < text.index("z-config.yaml")
    assert "mode: live" in build_qa(replace(inputs, run=replace(inputs.run, mode="live"))).to_markdown()
