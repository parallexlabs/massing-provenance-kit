"""Offline provenance regressions; all evidence and retrieval metadata are synthetic."""

import json
from dataclasses import fields

import pytest

from massing.heights import (
    EvidenceCandidate,
    EvidenceIssue,
    EvidenceSummary,
    HeightDecision,
    HeightSource,
    apply_overrides,
    parse_override_rows,
    resolve_height,
)

REF = "test-only://not-city-evidence"
HEIGHT_REF = "test-only://original-height"
LEVELS_REF = 'test-only://levels?note="synthetic"'
APPLICATION_REF = "test-only://application"
RAW = "bad test-only://invalid-height"
TEXT = "SYNTHETIC TEST ONLY: explicit height of 24 metres."
RETRIEVED = "not-applicable-synthetic"


@pytest.fixture
def provenance_base(typology):
    decision = resolve_height("b1", [
        EvidenceCandidate(HeightSource.CITY_SUPPLIED, RAW, reference=REF, test_only=True),
        EvidenceCandidate(HeightSource.OSM_HEIGHT, 24, reference=HEIGHT_REF,
                          source_text=TEXT, retrieved_at=RETRIEVED),
        EvidenceCandidate(HeightSource.OSM_LEVELS, 10, reference=LEVELS_REF),
        EvidenceCandidate(HeightSource.APPLICATION_STOREYS, 7,
                          reference=APPLICATION_REF,
                          source_text="SYNTHETIC TEST ONLY: seven storeys."),
    ], footprint_area_m2=100, use_class="office", typology=typology, min_height=2)
    assert (decision.source, decision.height_m, decision.confidence) == (
        HeightSource.OSM_HEIGHT, 24, .9,
    )
    assert len(decision.issues) == 1 and decision.issues[0].code == "malformed"
    assert RAW in decision.issues[0].raw_value
    assert [(a.source, a.height_m, a.reference) for a in decision.alternatives] == [
        (HeightSource.OSM_LEVELS, 32.5, LEVELS_REF),
        (HeightSource.APPLICATION_STOREYS, 23.2, APPLICATION_REF),
    ]
    assert not decision.history and not decision.test_only
    return decision


def round_trip(decision, label_rules):
    properties = json.loads(json.dumps(decision.to_properties(), allow_nan=False))
    assert properties["height_test_only"] is decision.test_only
    assert properties["height_issue_codes"] == ";".join(i.code for i in decision.issues)
    restored = HeightDecision.from_properties(decision.building_id, properties)
    for field in fields(HeightDecision):
        assert getattr(restored, field.name) == getattr(decision, field.name), field.name
    assert isinstance(restored.source, HeightSource)
    assert isinstance(restored.issues, tuple)
    assert all(isinstance(issue, EvidenceIssue) for issue in restored.issues)
    for summaries in (restored.alternatives, restored.history):
        assert isinstance(summaries, tuple)
        assert all(isinstance(s, EvidenceSummary) and isinstance(s.source, HeightSource)
                   for s in summaries)
    assert restored.label_key == decision.label_key
    assert label_rules.render(restored) == label_rules.render(decision)
    return restored


def override(decision, height, reference):
    records, issues = parse_override_rows([{
        "id": decision.building_id, "height_m": str(height),
        "evidence_reference": reference, "reviewed": "true", "test_only": "true",
        "reviewed_by": "synthetic-test-reviewer",
    }])
    assert not issues and len(records) == 1 and records[0].test_only
    result, issues = apply_overrides({decision.building_id: decision}, records)
    assert not issues
    return result[decision.building_id]


def test_current_evidence_metadata_issues_and_alternatives_round_trip(
    provenance_base, label_rules,
):
    restored = round_trip(provenance_base, label_rules)
    assert (restored.reference, restored.source_text, restored.retrieved_at) == (
        HEIGHT_REF, TEXT, RETRIEVED,
    )
    assert restored.min_height_m == 2 and restored.use_class == "commercial"
    assert restored.issues == provenance_base.issues
    assert restored.issues[0].raw_value == provenance_base.issues[0].raw_value
    assert round_trip(restored, label_rules) == restored


def test_reviewed_test_only_override_history_and_label_round_trip(provenance_base, label_rules):
    updated = override(provenance_base, 40, REF)
    assert updated.history == (provenance_base.summary(),)
    assert updated.issues == provenance_base.issues
    assert updated.alternatives == provenance_base.alternatives
    restored = round_trip(updated, label_rules)
    assert restored.test_only and restored.reference == REF
    assert restored.history[0].reference == HEIGHT_REF
    label = label_rules.render(restored)
    assert label.style_flag == "test_only" and "not City evidence" in label.text


def test_repeated_override_after_import_retains_original_evidence(provenance_base, label_rules):
    first = round_trip(override(provenance_base, 40, REF), label_rules)
    second = override(first, 45, REF + "/revision-2")
    assert first.height_m == 40 and provenance_base.height_m == 24
    assert (second.height_prev_m, second.height_prev_source,
            second.height_prev_confidence) == (24, HeightSource.OSM_HEIGHT, .9)
    assert second.history == (provenance_base.summary(), first.summary())
    restored = round_trip(second, label_rules)
    assert [s.reference for s in restored.history] == [HEIGHT_REF, REF]
    assert [s.reference for s in restored.alternatives] == [LEVELS_REF, APPLICATION_REF]
    assert restored.reference == REF + "/revision-2" and restored.test_only
    assert restored.issues == provenance_base.issues
    assert round_trip(restored, label_rules) == restored
