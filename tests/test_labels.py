from dataclasses import replace

import pytest

from massing.heights import EvidenceCandidate, HeightSource
from massing.labels import LABELS_CSV_COLUMNS, label_row, label_values

REF = "test-only://not-city-evidence"


@pytest.mark.parametrize("kind,text,style", [
    ("osm_height", "24.0 m - OpenStreetMap height tag (not surveyed)", "evidence"),
    ("osm_levels", "32.5 m - estimated from 10 OpenStreetMap levels x 3.1 m + 1.5 m",
     "estimated"),
    ("application_storeys",
     "32.5 m - estimated from 10 storeys in public application test-only://application",
     "estimated"),
    ("typology_default",
     "6.0 m - uncertain typology default for commercial; no height evidence", "uncertain"),
    ("unknown", "4.0 m placeholder - height unknown, no usable evidence", "uncertain"),
    ("city_supplied_test_only",
     "40.0 m - TEST-ONLY override, not City evidence (test-only://not-city-evidence); "
     "underlying osm_levels estimate 32.5 m", "test_only"),
    ("city_supplied",
     "40.0 m - reviewed City-supplied height (test-only://not-city-evidence); "
     "replaces osm_levels estimate of 32.5 m", "evidence"),
])
def test_every_exact_approved_label(decide, label_rules, kind, text, style):
    if kind == "osm_height":
        decision = decide([EvidenceCandidate(HeightSource.OSM_HEIGHT, 24)])
    elif kind == "osm_levels":
        decision = decide([EvidenceCandidate(HeightSource.OSM_LEVELS, 10)])
    elif kind == "application_storeys":
        decision = decide([EvidenceCandidate(
            HeightSource.APPLICATION_STOREYS, 10, reference="test-only://application",
            source_text="SYNTHETIC TEST ONLY: ten storeys",
        )])
    elif kind == "typology_default":
        decision = decide(use_class="commercial")
    elif kind == "unknown":
        decision = decide()
    else:
        decision = decide([
            EvidenceCandidate(HeightSource.OSM_LEVELS, 10),
            EvidenceCandidate(HeightSource.CITY_SUPPLIED, 40, reference=REF, test_only=True),
        ])
        if kind == "city_supplied":
            # Exercise approved wording only, not a real City-evidence fixture.
            decision = replace(decision, test_only=False)
    label = label_rules.render(decision)
    assert (label.rule_key, label.text, label.style_flag) == (kind, text, style)
    assert label_rules.rule_for(decision).key == kind


def test_test_only_label_and_csv_do_not_claim_city_evidence(decide, label_rules):
    decision = decide([
        EvidenceCandidate(HeightSource.OSM_LEVELS, 10),
        EvidenceCandidate(HeightSource.CITY_SUPPLIED, 40, reference=REF, test_only=True),
    ])
    label = label_rules.render(decision)
    result = label_row(decision, label)
    assert tuple(result) == LABELS_CSV_COLUMNS
    assert all(isinstance(value, str) for value in result.values())
    assert (result["height_m"], result["min_height_m"], result["height_prev_m"]) == (
        "40.000", "0.000", "32.500",
    )
    assert result["height_confidence"] == "1.0"
    assert result["height_reference"] == REF
    assert result["style_flag"] == "test_only"
    assert "not City evidence" in result["label_text"]
    assert label_values(decision)["height_prev_source"] == "osm_levels"


def test_unknown_csv_empty_optional_values(decide, label_rules):
    decision = decide()
    result = label_row(decision, label_rules.render(decision))
    assert result["height_reference"] == result["height_prev_m"] == ""
    assert result["height_confidence"] == "0.1"
    assert result["style_flag"] == "uncertain"


def test_render_rejects_missing_required_value(decide, label_rules):
    decision = decide([EvidenceCandidate(HeightSource.OSM_LEVELS, 10)])
    with pytest.raises(ValueError, match="storeys"):
        label_rules.render(replace(decision, storeys=None))
