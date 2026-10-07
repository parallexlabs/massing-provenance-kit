from dataclasses import replace

import pytest

from massing.heights import (
    CONFIDENCE,
    LADDER,
    EvidenceCandidate,
    EvidenceError,
    HeightDecision,
    HeightSource,
    apply_overrides,
    parse_bool,
    parse_count,
    parse_length_m,
    parse_override_rows,
    storeys_to_height,
    validate_building_id,
)

REF = "test-only://not-city-evidence"
CITY = HeightSource.CITY_SUPPLIED
HEIGHT = HeightSource.OSM_HEIGHT
LEVELS = HeightSource.OSM_LEVELS
APPLICATION = HeightSource.APPLICATION_STOREYS


def row(**changes):
    result = {
        "id": "b1", "height_m": "40", "evidence_reference": REF,
        "reviewed": "true", "test_only": "true",
    }
    result.update(changes)
    return result


def evidence():
    return [
        EvidenceCandidate(CITY, 40, reference=REF, test_only=True),
        EvidenceCandidate(HEIGHT, 24),
        EvidenceCandidate(LEVELS, 10),
        EvidenceCandidate(APPLICATION, 7, reference="test-only://application",
                          source_text="SYNTHETIC TEST ONLY: seven storeys"),
    ]


def codes(issues):
    return [issue.code for issue in issues]


@pytest.mark.parametrize("start,source,height,confidence", [
    (0, CITY, 40, 1.0),
    (1, HEIGHT, 24, 0.9),
    (2, LEVELS, 32.5, 0.7),
    (3, APPLICATION, 23.2, 0.6),
])
def test_first_usable_ladder_independent_of_input_order(
    decide, start, source, height, confidence,
):
    decision = decide(tuple(reversed(evidence()[start:])), use_class="commercial")
    assert (decision.source, decision.height_m, decision.confidence) == (
        source, height, confidence,
    )
    assert not decision.is_uncertain
    if source is CITY:
        assert (decision.height_prev_m, decision.height_prev_source) == (24, HEIGHT)


def test_declared_ladder_and_confidence():
    assert tuple(source.value for source in LADDER) == (
        "city_supplied", "osm_height", "osm_levels", "application_storeys",
        "typology_default", "unknown",
    )
    assert tuple(CONFIDENCE[source] for source in LADDER) == (1, .9, .7, .6, .3, .1)


def test_invalid_higher_rungs_and_lower_evidence_are_reported(decide):
    candidates = [
        EvidenceCandidate(CITY, "bad", reference=REF, test_only=True),
        EvidenceCandidate(HEIGHT, "NaN"),
        EvidenceCandidate(LEVELS, 10),
        EvidenceCandidate(APPLICATION, 0, reference="test-only://application"),
    ]
    decision = decide(candidates)
    assert (decision.source, decision.height_m, decision.storeys) == (LEVELS, 32.5, 10)
    assert set(codes(decision.issues)) == {"malformed", "nonfinite", "nonpositive"}
    assert all(issue.building_id == "b1" and issue.message for issue in decision.issues)


@pytest.mark.parametrize("raw,unit,expected", [
    (12, None, 12), (" 12.25 M ", None, 12.25), ("10 metres", None, 10),
    ("10 meters", "metre", 10), ("10 ft", None, 3.048),
    ("10", "feet", 3.048), ("10 foot", "ft", 3.048),
])
def test_supported_lengths(raw, unit, expected):
    assert parse_length_m(raw, unit) == expected


@pytest.mark.parametrize("raw,unit,code", [
    (None, None, "malformed"), (True, None, "malformed"),
    ("", None, "malformed"), ("12;13", None, "malformed"),
    ("1e2", None, "malformed"), ("NaN", None, "nonfinite"),
    (float("inf"), None, "nonfinite"), (0, None, "nonpositive"),
    (-1, None, "nonpositive"), ("10 cm", None, "unsupported_unit"),
    (10, "yards", "unsupported_unit"), ("10 ft", "m", "conflicting_unit"),
])
def test_invalid_lengths(raw, unit, code):
    with pytest.raises(EvidenceError) as caught:
        parse_length_m(raw, unit)
    assert caught.value.code == code


@pytest.mark.parametrize("raw", [10, 10.0, "10", " 10.0 "])
def test_ten_levels(raw):
    assert parse_count(raw) == 10
    assert storeys_to_height(parse_count(raw)) == 32.5


@pytest.mark.parametrize("raw,code", [
    (False, "malformed"), (None, "malformed"), ("10 levels", "malformed"),
    (0, "nonpositive"), (-2, "nonpositive"), (2.5, "non_integer"),
    ("inf", "nonfinite"),
])
def test_invalid_counts(raw, code):
    with pytest.raises(EvidenceError) as caught:
        parse_count(raw)
    assert caught.value.code == code


def test_outlier_storeys_are_not_clamped(decide):
    decision = decide([EvidenceCandidate(LEVELS, 61)])
    assert (decision.storeys, decision.height_m) == (61, 190.6)


@pytest.mark.parametrize("raw", ["", " b1", "b 1", "../b1", "a/b", "a" * 129, None, 1])
def test_invalid_ids(raw, decide):
    with pytest.raises(EvidenceError, match="building id"):
        validate_building_id(raw)
    with pytest.raises(EvidenceError):
        decide([], building_id=raw)


def test_safe_id_and_candidate_constraints():
    assert validate_building_id("way:1.part-2_3") == "way:1.part-2_3"
    with pytest.raises(ValueError):
        EvidenceCandidate(HeightSource.UNKNOWN, 4)
    with pytest.raises(ValueError):
        EvidenceCandidate(HEIGHT, 10, test_only=True)


@pytest.mark.parametrize("raw,expected", [("YES", True), (" 1 ", True), ("no", False)])
def test_boolean_csv_tokens(raw, expected):
    assert parse_bool(raw) is expected


@pytest.mark.parametrize("raw", ["maybe", "", None, True, 1])
def test_invalid_boolean_tokens(raw):
    with pytest.raises(EvidenceError) as caught:
        parse_bool(raw)
    assert caught.value.code == "invalid_boolean"


@pytest.mark.parametrize("source", [CITY, APPLICATION])
@pytest.mark.parametrize("reference,code", [
    (None, "missing_reference"), (" ", "missing_reference"),
    ("two\nlines", "malformed"), ("x" * 301, "malformed"), (3, "malformed"),
])
def test_required_reference_validation(decide, source, reference, code):
    candidate = EvidenceCandidate(source, 10, reference=reference, test_only=source is CITY)
    decision = decide([candidate])
    assert decision.source is HeightSource.UNKNOWN
    assert codes(decision.issues) == [code]


@pytest.mark.parametrize("use_class,height,source,confidence", [
    (None, 4, HeightSource.UNKNOWN, .1),
    ("residential", 4, HeightSource.UNKNOWN, .1),
    ("yes", 4, HeightSource.UNKNOWN, .1),
    (" OFFICE ", 6, HeightSource.TYPOLOGY_DEFAULT, .3),
])
def test_unknown_and_explicit_typology(decide, use_class, height, source, confidence):
    decision = decide(use_class=use_class)
    assert (decision.height_m, decision.source, decision.confidence) == (
        height, source, confidence,
    )
    assert decision.is_uncertain
    if use_class in {"yes", "residential"}:
        assert codes(decision.issues) == ["unknown_use_class"]


@pytest.mark.parametrize("area", [0, -1, float("nan"), float("inf")])
def test_invalid_typology_area_reports_and_falls_back(decide, area):
    decision = decide(use_class="commercial", footprint_area_m2=area)
    assert decision.source is HeightSource.UNKNOWN
    assert codes(decision.issues) == ["invalid_area"]


def test_alternatives_and_application_provenance(decide):
    decision = decide(evidence()[1:])
    assert [(a.source, a.height_m, a.confidence) for a in decision.alternatives] == [
        (LEVELS, 32.5, .7), (APPLICATION, 23.2, .6),
    ]
    application = decide(evidence()[3:])
    assert application.reference == "test-only://application"
    assert application.source_text == "SYNTHETIC TEST ONLY: seven storeys"
    assert application.retrieved_at is None


@pytest.mark.parametrize("raw,expected", [(None, 0), (0, 0), ("0", 0), ("10 ft", 3.048)])
def test_valid_min_height(decide, raw, expected):
    assert decide([EvidenceCandidate(HEIGHT, 24)], min_height=raw).min_height_m == expected


@pytest.mark.parametrize("raw", [-1, "NaN", "4 cm", 24, 25, False])
def test_invalid_min_height_is_visible(decide, raw):
    decision = decide([EvidenceCandidate(HEIGHT, 24)], min_height=raw)
    assert decision.min_height_m == 0
    assert len(decision.issues) == 1
    assert decision.issues[0].source == "min_height"


@pytest.mark.parametrize("raw", ["0.0", "0 m", "0 ft"])
def test_zero_min_height_with_supported_representation(decide, raw):
    decision = decide([EvidenceCandidate(HEIGHT, 24)], min_height=raw)
    assert decision.min_height_m == 0
    assert not decision.issues


def test_min_height_validated_against_winning_override(decide):
    decision = decide([evidence()[0]], min_height=10)
    assert (decision.height_m, decision.min_height_m) == (40, 10)
    assert not decision.issues


def test_fixture_override_preserves_original_estimate(decide, override_rows):
    records, issues = parse_override_rows(override_rows)
    assert not issues and len(records) == 1
    assert records[0].test_only and records[0].evidence_reference == REF
    original = decide([EvidenceCandidate(LEVELS, 10)])
    result, issues = apply_overrides({"b1": original}, records)
    updated = result["b1"]
    assert not issues and original.height_m == 32.5
    assert (updated.height_m, updated.source, updated.confidence) == (40, CITY, 1)
    assert (updated.height_prev_m, updated.height_prev_source,
            updated.height_prev_confidence) == (32.5, LEVELS, .7)
    assert updated.label_key == "city_supplied_test_only"
    assert updated.history == (original.summary(),)
    again, issues = apply_overrides(result, records)
    assert not issues and again["b1"].height_prev_m == 32.5
    assert len(again["b1"].history) == 2


@pytest.mark.parametrize("changes,code", [
    ({"reviewed": "false"}, "unreviewed_override"),
    ({"id": "bad/id"}, "invalid_id"), ({"height_m": "-2"}, "nonpositive"),
    ({"height_m": "nan"}, "nonfinite"), ({"unit": "cm"}, "unsupported_unit"),
    ({"evidence_reference": ""}, "missing_reference"),
    ({"test_only": "maybe"}, "invalid_boolean"),
    ({"reviewed_by": "two\nlines"}, "malformed"),
])
def test_bad_override_rows(changes, code):
    records, issues = parse_override_rows([row(**changes)])
    assert not records and codes(issues) == [code]
    assert issues[0].row == 2 and issues[0].source == CITY.value


def test_missing_column_and_duplicate_overrides():
    incomplete = row()
    del incomplete["reviewed"]
    records, issues = parse_override_rows([incomplete])
    assert not records and codes(issues) == ["missing_column"]
    records, issues = parse_override_rows([row(), row(height_m="41")])
    assert not records and codes(issues) == ["duplicate_override_id"] * 2
    assert {issue.row for issue in issues} == {2, 3}


def test_duplicate_id_with_invalid_sibling_is_not_applied():
    records, issues = parse_override_rows([row(), row(height_m="bad")])
    assert not records
    assert "duplicate_override_id" in codes(issues)


def test_unmatched_and_too_low_override_are_visible(decide):
    original = decide([EvidenceCandidate(HEIGHT, 24)], min_height=10)
    records, issues = parse_override_rows([row(id="missing"), row(height_m="9")])
    assert not issues
    result, issues = apply_overrides({"b1": original}, records)
    assert result == {"b1": original}
    assert codes(issues) == ["unmatched_override_id", "invalid_min_height"]
    assert [issue.row for issue in issues] == [2, 3]


def test_properties_round_trip(decide):
    for candidates in [(), evidence()[1:], evidence()[2:], evidence()[3:], evidence()]:
        decision = decide(candidates)
        properties = decision.to_properties()
        rebuilt = HeightDecision.from_properties("b1", properties)
        assert rebuilt.to_properties() == properties
        assert rebuilt.summary() == decision.summary()
        assert rebuilt.label_key == decision.label_key


@pytest.mark.parametrize("key,value", [
    ("height_m", True), ("height_m", "12"), ("height_m", float("nan")),
    ("height_m", 0), ("min_height_m", -1), ("min_height_m", 24),
    ("height_source", "fiction"), ("height_confidence", .1),
    ("height_reference", 9), ("height_storeys", 2.5),
])
def test_invalid_properties(decide, key, value):
    properties = decide([EvidenceCandidate(HEIGHT, 24)]).to_properties()
    properties[key] = value
    with pytest.raises(EvidenceError) as caught:
        HeightDecision.from_properties("b1", properties)
    assert caught.value.code == "invalid_properties"


def test_properties_require_height_and_boolean_test_only(decide):
    properties = decide(evidence()).to_properties()
    del properties["height_m"]
    with pytest.raises(EvidenceError):
        HeightDecision.from_properties("b1", properties)
    properties = decide(evidence()).to_properties()
    properties["height_test_only"] = "true"
    with pytest.raises(EvidenceError):
        HeightDecision.from_properties("b1", properties)


def test_decision_invariants(decide):
    original = decide()
    for changes in [{"height_m": -1}, {"min_height_m": 4}, {"confidence": .9},
                    {"test_only": True}, {"source": CITY, "confidence": 1}]:
        with pytest.raises(ValueError):
            replace(original, **changes)
