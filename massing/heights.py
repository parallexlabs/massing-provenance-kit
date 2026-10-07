"""Height evidence ladder: parsing, validation, resolution and overrides (pure).

Ladder (first usable evidence wins): city_supplied 1.0, osm_height 0.9,
osm_levels 0.7, application_storeys 0.6, typology_default 0.3, unknown 0.1.
Confidence is a declared evidence score, not a statistical probability.
Units: metres (m, metre(s), meter(s); default when absent) and international
feet (ft, foot, feet; x 0.3048). Anything else is rejected and reported.
Heights must be > 0; min_height may be 0 in any supported unit, never negative.
min_height is validated against the final (winning) height, including overrides.
Storeys: n x 3.1 m + 1.5 m ground-floor allowance (10 -> 32.5 m).
Storey counts above 60 are retained unchanged; QA flags them.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from massing import ConfigError, load_yaml_mapping

STOREY_HEIGHT_M = 3.1
GROUND_ALLOWANCE_M = 1.5
UNKNOWN_HEIGHT_M = 4.0
STOREY_OUTLIER_THRESHOLD = 60
HEIGHT_DECIMALS = 3


class HeightSource(StrEnum):
    CITY_SUPPLIED = "city_supplied"
    OSM_HEIGHT = "osm_height"
    OSM_LEVELS = "osm_levels"
    APPLICATION_STOREYS = "application_storeys"
    TYPOLOGY_DEFAULT = "typology_default"
    UNKNOWN = "unknown"


LADDER: tuple[HeightSource, ...] = tuple(HeightSource)
CONFIDENCE: Mapping[HeightSource, float] = {
    HeightSource.CITY_SUPPLIED: 1.0,
    HeightSource.OSM_HEIGHT: 0.9,
    HeightSource.OSM_LEVELS: 0.7,
    HeightSource.APPLICATION_STOREYS: 0.6,
    HeightSource.TYPOLOGY_DEFAULT: 0.3,
    HeightSource.UNKNOWN: 0.1,
}
UNCERTAIN_SOURCES = frozenset({HeightSource.TYPOLOGY_DEFAULT, HeightSource.UNKNOWN})
CANDIDATE_SOURCES = frozenset(LADDER[:4])
UNIT_FACTORS: Mapping[str, float] = {
    "m": 1.0, "metre": 1.0, "metres": 1.0, "meter": 1.0, "meters": 1.0,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
}
_RANK = {source: rank for rank, source in enumerate(LADDER)}
_NUMBER = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z]*)")
_COUNT = re.compile(r"[+-]?\d+(?:\.\d+)?")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_NONFINITE = frozenset({"nan", "inf", "infinity"})
_TRUE, _FALSE = frozenset({"true", "yes", "1"}), frozenset({"false", "no", "0"})
OVERRIDE_REQUIRED_COLUMNS = ("id", "height_m", "evidence_reference", "reviewed", "test_only")
OVERRIDE_OPTIONAL_COLUMNS = ("unit", "reviewed_by")
_ISSUE_KEYS = frozenset({"building_id", "source", "code", "message", "raw_value", "row"})
_SUMMARY_KEYS = frozenset({"source", "height_m", "confidence", "derivation", "reference",
                           "storeys"})


class EvidenceError(ValueError):
    """Invalid evidence; ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvidenceIssue:
    building_id: str
    source: str
    code: str
    message: str
    raw_value: str
    row: int | None = None


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    source: HeightSource
    value: object
    unit: str | None = None
    reference: str | None = None
    source_text: str | None = None
    retrieved_at: str | None = None
    test_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source, HeightSource) or self.source not in CANDIDATE_SOURCES:
            raise ValueError(f"{self.source!r} is derived, not a candidate evidence source")
        if self.test_only and self.source is not HeightSource.CITY_SUPPLIED:
            raise ValueError("test_only applies only to city_supplied overrides")


@dataclass(frozen=True, slots=True)
class EvidenceSummary:
    source: HeightSource
    height_m: float
    confidence: float
    derivation: str
    reference: str | None = None
    storeys: int | None = None


@dataclass(frozen=True, slots=True)
class OverrideRecord:
    building_id: str
    height_m: float
    evidence_reference: str
    test_only: bool
    row: int
    reviewed_by: str | None = None


def _raw(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def validate_building_id(raw: object) -> str:
    if not isinstance(raw, str) or _ID.fullmatch(raw) is None:
        raise EvidenceError("invalid_id", f"building id {_raw(raw)} must match {_ID.pattern}")
    return raw


def _finite(raw: int | float) -> float:
    try:
        value = float(raw)
    except OverflowError as exc:
        raise EvidenceError("nonfinite", f"value {_raw(raw)} overflows") from exc
    if not math.isfinite(value):
        raise EvidenceError("nonfinite", f"value {_raw(raw)} is not finite")
    return value


def _number(raw: object, pattern: re.Pattern[str]) -> tuple[float, str]:
    if raw is None or isinstance(raw, bool):
        raise EvidenceError("malformed", f"{_raw(raw)} is not a number")
    if isinstance(raw, int | float):
        return _finite(raw), ""
    if not isinstance(raw, str):
        raise EvidenceError("malformed", f"{_raw(raw)} is not a number")
    text = raw.strip()
    match = pattern.fullmatch(text)
    if match is None:
        code = "nonfinite" if text.lower().lstrip("+-") in _NONFINITE else "malformed"
        raise EvidenceError(code, f"{_raw(raw)} is not a plain decimal number")
    number = match.group(1) if pattern.groups >= 1 else match.group(0)
    unit = (match.group(2) or "").lower() if pattern.groups >= 2 else ""
    return _finite(float(number)), unit


def _length(raw: object, unit: object, *, allow_zero: bool) -> float:
    value, text_unit = _number(raw, _NUMBER)
    if unit is not None and not isinstance(unit, str):
        raise EvidenceError("unsupported_unit", f"unit {_raw(unit)} is not supported")
    declared = (unit or "").strip().lower()
    for candidate in (text_unit, declared):
        if candidate and candidate not in UNIT_FACTORS:
            raise EvidenceError("unsupported_unit", f"unit {candidate!r} is not supported")
    if text_unit and declared and UNIT_FACTORS[text_unit] != UNIT_FACTORS[declared]:
        raise EvidenceError("conflicting_unit", f"{_raw(raw)} conflicts with unit {declared!r}")
    metres = round(value * UNIT_FACTORS[text_unit or declared or "m"], HEIGHT_DECIMALS)
    if allow_zero:
        if value < 0 or metres < 0:
            raise EvidenceError("negative", f"length {_raw(raw)} must be >= 0")
    elif metres <= 0:
        raise EvidenceError("nonpositive", f"length {_raw(raw)} must be > 0")
    return metres + 0.0


def parse_length_m(raw: object, unit: str | None = None) -> float:
    """Parse a positive finite length to metres (3 dp). Raises EvidenceError."""
    return _length(raw, unit, allow_zero=False)


def parse_min_height_m(raw: object, unit: str | None = None) -> float:
    """Parse a finite nonnegative min_height to metres; zero in any supported unit is valid."""
    return _length(raw, unit, allow_zero=True)


def parse_count(raw: object) -> int:
    """Parse a positive integer storey/level count. Raises EvidenceError."""
    value, _ = _number(raw, _COUNT)
    if value <= 0:
        raise EvidenceError("nonpositive", f"count {_raw(raw)} must be > 0")
    if not value.is_integer():
        raise EvidenceError("non_integer", f"count {_raw(raw)} must be a whole number")
    return int(value)


def storeys_to_height(storeys: int) -> float:
    if isinstance(storeys, bool) or not isinstance(storeys, int) or storeys <= 0:
        raise EvidenceError("nonpositive", f"storeys {_raw(storeys)} must be a positive int")
    return round(storeys * STOREY_HEIGHT_M + GROUND_ALLOWANCE_M, HEIGHT_DECIMALS)


def parse_bool(raw: object) -> bool:
    text = raw.strip().lower() if isinstance(raw, str) else None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise EvidenceError("invalid_boolean", f"{_raw(raw)} is not true/false/yes/no/1/0")


def _reference(raw: object, *, required: bool) -> str | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise EvidenceError("missing_reference", "evidence reference is required")
        return None
    if not isinstance(raw, str) or "\n" in raw or "\r" in raw or len(raw.strip()) > 300:
        raise EvidenceError("malformed", f"reference {_raw(raw)} must be one line <= 300 chars")
    return raw.strip()


def _prop_float(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise TypeError(f"expected number, got {_raw(raw)}")
    return _finite(raw)


def _opt_prop_float(raw: object) -> float | None:
    return None if raw is None else _prop_float(raw)


def _opt_prop_int(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise TypeError(f"expected positive integer, got {_raw(raw)}")
    return raw


def _prop_str(raw: object) -> str | None:
    if raw is None or isinstance(raw, str):
        return raw
    raise TypeError(f"expected string, got {_raw(raw)}")


def _required_str(raw: Mapping[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string, got {_raw(value)}")
    return value


def _prop_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    raise TypeError(f"expected JSON boolean, got {_raw(raw)}")


def _prop_list(raw: object) -> list[object]:
    if not isinstance(raw, list):
        raise TypeError(f"expected list, got {_raw(raw)}")
    return raw


def _issue_to_dict(issue: EvidenceIssue) -> dict[str, object]:
    return {"building_id": issue.building_id, "source": issue.source, "code": issue.code,
            "message": issue.message, "raw_value": issue.raw_value, "row": issue.row}


def _issue_from_dict(raw: object) -> EvidenceIssue:
    if not isinstance(raw, dict) or set(raw) != _ISSUE_KEYS:
        raise TypeError(f"issue must have exactly {sorted(_ISSUE_KEYS)}")
    return EvidenceIssue(
        building_id=_required_str(raw, "building_id"),
        source=_required_str(raw, "source"),
        code=_required_str(raw, "code"),
        message=_required_str(raw, "message"),
        raw_value=_required_str(raw, "raw_value"),
        row=_opt_prop_int(raw["row"]),
    )


def _summary_to_dict(summary: EvidenceSummary) -> dict[str, object]:
    return {"source": summary.source.value, "height_m": summary.height_m,
            "confidence": summary.confidence, "derivation": summary.derivation,
            "reference": summary.reference, "storeys": summary.storeys}


def _summary_from_dict(raw: object) -> EvidenceSummary:
    if not isinstance(raw, dict) or set(raw) != _SUMMARY_KEYS:
        raise TypeError(f"evidence summary must have exactly {sorted(_SUMMARY_KEYS)}")
    source = HeightSource(raw["source"])
    height = _prop_float(raw["height_m"])
    confidence = _prop_float(raw["confidence"])
    derivation = raw["derivation"]
    if height <= 0 or confidence != CONFIDENCE[source] or not isinstance(derivation, str):
        raise ValueError(f"invalid evidence summary {_raw(raw)}")
    return EvidenceSummary(source, height, confidence, derivation,
                           _prop_str(raw["reference"]), _opt_prop_int(raw["storeys"]))


@dataclass(frozen=True, slots=True)
class HeightDecision:
    building_id: str
    height_m: float
    min_height_m: float
    source: HeightSource
    confidence: float
    derivation: str
    reference: str | None = None
    source_text: str | None = None
    retrieved_at: str | None = None
    storeys: int | None = None
    use_class: str | None = None
    test_only: bool = False
    height_prev_m: float | None = None
    height_prev_source: HeightSource | None = None
    height_prev_confidence: float | None = None
    issues: tuple[EvidenceIssue, ...] = ()
    alternatives: tuple[EvidenceSummary, ...] = ()
    history: tuple[EvidenceSummary, ...] = ()

    def __post_init__(self) -> None:
        validate_building_id(self.building_id)
        if not (math.isfinite(self.height_m) and self.height_m > 0):
            raise ValueError(f"{self.building_id}: height_m must be finite and > 0")
        if not (math.isfinite(self.min_height_m) and 0 <= self.min_height_m < self.height_m):
            raise ValueError(f"{self.building_id}: need 0 <= min_height_m < height_m")
        if self.confidence != CONFIDENCE[self.source]:
            raise ValueError(f"{self.building_id}: confidence must equal ladder value")
        if self.test_only and self.source is not HeightSource.CITY_SUPPLIED:
            raise ValueError(f"{self.building_id}: test_only requires city_supplied")
        if self.source is HeightSource.CITY_SUPPLIED and (
            self.height_prev_m is None or self.height_prev_source is None
        ):
            raise ValueError(f"{self.building_id}: override must retain the prior estimate")
        if self.height_prev_source is not None and (
            self.height_prev_m is None or not math.isfinite(self.height_prev_m)
            or self.height_prev_m <= 0
            or self.height_prev_confidence != CONFIDENCE[self.height_prev_source]
        ):
            raise ValueError(f"{self.building_id}: inconsistent prior estimate")

    @property
    def is_uncertain(self) -> bool:
        return self.source in UNCERTAIN_SOURCES

    @property
    def label_key(self) -> str:
        return "city_supplied_test_only" if self.test_only else self.source.value

    def summary(self) -> EvidenceSummary:
        return EvidenceSummary(self.source, self.height_m, self.confidence,
                               self.derivation, self.reference, self.storeys)

    def to_properties(self) -> dict[str, object]:
        prev = self.height_prev_source
        return {
            "height_m": self.height_m,
            "min_height_m": self.min_height_m,
            "height_source": self.source.value,
            "height_confidence": self.confidence,
            "height_derivation": self.derivation,
            "height_reference": self.reference,
            "height_source_text": self.source_text,
            "height_retrieved_at": self.retrieved_at,
            "height_storeys": self.storeys,
            "height_use_class": self.use_class,
            "height_test_only": self.test_only,
            "height_prev_m": self.height_prev_m,
            "height_prev_source": prev.value if prev is not None else None,
            "height_prev_confidence": self.height_prev_confidence,
            "height_issue_codes": ";".join(issue.code for issue in self.issues),
            "height_issues": [_issue_to_dict(i) for i in self.issues],
            "height_alternatives": [_summary_to_dict(s) for s in self.alternatives],
            "height_history": [_summary_to_dict(s) for s in self.history],
        }

    @classmethod
    def from_properties(cls, building_id: str, props: Mapping[str, Any]) -> HeightDecision:
        """Strict inverse of to_properties(); every key is required, nothing is coerced."""
        try:
            prev = props["height_prev_source"]
            decision = cls(
                building_id=building_id,
                height_m=_prop_float(props["height_m"]),
                min_height_m=_prop_float(props["min_height_m"]),
                source=HeightSource(props["height_source"]),
                confidence=_prop_float(props["height_confidence"]),
                derivation=str(_prop_str(props["height_derivation"])),
                reference=_prop_str(props["height_reference"]),
                source_text=_prop_str(props["height_source_text"]),
                retrieved_at=_prop_str(props["height_retrieved_at"]),
                storeys=_opt_prop_int(props["height_storeys"]),
                use_class=_prop_str(props["height_use_class"]),
                test_only=_prop_bool(props["height_test_only"]),
                height_prev_m=_opt_prop_float(props["height_prev_m"]),
                height_prev_source=None if prev is None else HeightSource(prev),
                height_prev_confidence=_opt_prop_float(props["height_prev_confidence"]),
                issues=tuple(_issue_from_dict(i) for i in _prop_list(props["height_issues"])),
                alternatives=tuple(_summary_from_dict(s)
                                   for s in _prop_list(props["height_alternatives"])),
                history=tuple(_summary_from_dict(s)
                              for s in _prop_list(props["height_history"])),
            )
            if props["height_derivation"] is None:
                raise TypeError("height_derivation is required")
            if props["height_issue_codes"] != ";".join(i.code for i in decision.issues):
                raise ValueError("height_issue_codes does not match height_issues")
            return decision
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceError("invalid_properties", f"{building_id}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class TypologyBand:
    max_area_m2: float | None
    height_m: float


@dataclass(frozen=True, slots=True)
class TypologyDefaults:
    classes: Mapping[str, tuple[TypologyBand, ...]]
    aliases: Mapping[str, str]
    origin: str = "mapping"

    def resolve_use_class(self, raw: object) -> str | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        key = raw.strip().lower()
        return key if key in self.classes else self.aliases.get(key)

    def height_for(self, area_m2: float, use_class: str) -> float:
        if not (math.isfinite(area_m2) and area_m2 > 0):
            raise EvidenceError("invalid_area", f"footprint area {_raw(area_m2)} must be > 0")
        for band in self.classes[use_class]:
            if band.max_area_m2 is None or area_m2 <= band.max_area_m2:
                return band.height_m
        raise ConfigError(f"use class {use_class!r} lacks an unbounded final band")


def typology_from_mapping(data: Mapping[str, Any], origin: str = "mapping") -> TypologyDefaults:
    raw_classes = data.get("classes")
    if not isinstance(raw_classes, dict) or not raw_classes:
        raise ConfigError(f"{origin}: 'classes' must be a non-empty mapping")
    classes: dict[str, tuple[TypologyBand, ...]] = {}
    for name, bands in raw_classes.items():
        if not isinstance(name, str) or name != name.strip().lower() or not name:
            raise ConfigError(f"{origin}: class name {name!r} must be lowercase")
        if not isinstance(bands, list) or not bands:
            raise ConfigError(f"{origin}: class {name!r} needs a list of bands")
        parsed: list[TypologyBand] = []
        for index, band in enumerate(bands):
            if not isinstance(band, dict) or set(band) != {"max_area_m2", "height_m"}:
                raise ConfigError(f"{origin}: {name}[{index}] needs max_area_m2 and height_m")
            limit, height = band["max_area_m2"], band["height_m"]
            last = index == len(bands) - 1
            try:
                h = _prop_float(height)
                lim = None if limit is None else _prop_float(limit)
            except (TypeError, EvidenceError) as exc:
                raise ConfigError(f"{origin}: {name}[{index}]: {exc}") from exc
            if h <= 0 or (lim is not None and lim <= 0) or ((lim is None) != last):
                raise ConfigError(f"{origin}: {name}[{index}] invalid; only last band is null")
            if parsed and lim is not None and parsed[-1].max_area_m2 is not None \
                    and lim <= parsed[-1].max_area_m2:
                raise ConfigError(f"{origin}: {name} bands must have increasing max_area_m2")
            parsed.append(TypologyBand(lim, h))
        classes[name] = tuple(parsed)
    raw_aliases = data.get("aliases", {})
    if not isinstance(raw_aliases, dict):
        raise ConfigError(f"{origin}: 'aliases' must be a mapping")
    aliases: dict[str, str] = {}
    for alias, target in raw_aliases.items():
        if not isinstance(alias, str) or not isinstance(target, str) or target not in classes:
            raise ConfigError(f"{origin}: alias {alias!r} -> {target!r} is not a known class")
        aliases[alias.strip().lower()] = target
    return TypologyDefaults(classes, aliases, origin)


def load_typology_defaults(path: Path | None = None) -> TypologyDefaults:
    data, origin = load_yaml_mapping("typology_defaults.yaml", path)
    return typology_from_mapping(data, origin)


def _evaluate(candidate: EvidenceCandidate) -> tuple[float, str, int | None, str | None]:
    source = candidate.source
    if source in (HeightSource.OSM_LEVELS, HeightSource.APPLICATION_STOREYS):
        required = source is HeightSource.APPLICATION_STOREYS
        reference = _reference(candidate.reference, required=required)
        storeys = parse_count(candidate.value)
        height = storeys_to_height(storeys)
        return height, f"{storeys} x {STOREY_HEIGHT_M} m + {GROUND_ALLOWANCE_M} m", storeys, reference
    reference = _reference(candidate.reference, required=source is HeightSource.CITY_SUPPLIED)
    height = parse_length_m(candidate.value, candidate.unit)
    return height, f"explicit height {height} m", None, reference


def _issue(bid: str, source: str, exc: EvidenceError, raw: object, row: int | None = None
           ) -> EvidenceIssue:
    return EvidenceIssue(bid, source, exc.code, str(exc), _raw(raw), row)


def _override(decision: HeightDecision, height: float, reference: str, test_only: bool,
              derivation: str) -> HeightDecision:
    if decision.min_height_m >= height:
        raise EvidenceError("invalid_min_height",
                            f"override {height} m is not above min_height_m {decision.min_height_m}")
    if decision.source is HeightSource.CITY_SUPPLIED:
        prev_m, prev_src = decision.height_prev_m, decision.height_prev_source
        prev_conf = decision.height_prev_confidence
    else:
        prev_m, prev_src, prev_conf = decision.height_m, decision.source, decision.confidence
    return replace(
        decision, height_m=height, source=HeightSource.CITY_SUPPLIED, confidence=1.0,
        derivation=derivation, reference=reference, source_text=None, retrieved_at=None,
        storeys=None, test_only=test_only, height_prev_m=prev_m, height_prev_source=prev_src,
        height_prev_confidence=prev_conf, history=(*decision.history, decision.summary()),
    )


def resolve_height(
    building_id: str,
    candidates: Sequence[EvidenceCandidate],
    *,
    footprint_area_m2: float,
    use_class: str | None,
    typology: TypologyDefaults,
    min_height: object = None,
) -> HeightDecision:
    """Apply the ladder. Every candidate is validated; invalid ones become issues."""
    bid = validate_building_id(building_id)
    issues: list[EvidenceIssue] = []
    usable: list[tuple[EvidenceCandidate, float, str, int | None, str | None]] = []
    for cand in sorted(candidates, key=lambda c: _RANK[c.source]):
        try:
            usable.append((cand, *_evaluate(cand)))
        except EvidenceError as exc:
            issues.append(_issue(bid, cand.source.value, exc, cand.value))
    base = [u for u in usable if u[0].source is not HeightSource.CITY_SUPPLIED]
    overrides = [u for u in usable if u[0].source is HeightSource.CITY_SUPPLIED]
    resolved_class = typology.resolve_use_class(use_class)
    if use_class and resolved_class is None:
        issues.append(EvidenceIssue(bid, HeightSource.TYPOLOGY_DEFAULT.value, "unknown_use_class",
                                    f"use class {use_class!r} is not configured", _raw(use_class)))
    source, height, derivation = HeightSource.UNKNOWN, UNKNOWN_HEIGHT_M, "flat 4 m placeholder"
    storeys: int | None = None
    reference = text = retrieved = None
    if base:
        cand, height, derivation, storeys, reference = base[0]
        source, text, retrieved = cand.source, cand.source_text, cand.retrieved_at
    elif resolved_class is not None:
        try:
            height = typology.height_for(footprint_area_m2, resolved_class)
            source = HeightSource.TYPOLOGY_DEFAULT
            derivation = f"typology {resolved_class} by area {round(footprint_area_m2, 1)} m2"
        except EvidenceError as exc:
            issues.append(_issue(bid, HeightSource.TYPOLOGY_DEFAULT.value, exc, footprint_area_m2))
    floor = 0.0
    if min_height is not None:
        try:
            floor = parse_min_height_m(min_height)
        except EvidenceError as exc:
            issues.append(_issue(bid, "min_height", exc, min_height))
    winner: tuple[EvidenceCandidate, float, str | None] | None = None
    for cand, o_height, _, _, o_ref in overrides:
        if o_height > floor:
            winner = (cand, o_height, o_ref)
            break
        issues.append(EvidenceIssue(bid, HeightSource.CITY_SUPPLIED.value, "invalid_min_height",
                                    f"override {o_height} m is not above min_height {floor} m",
                                    _raw(cand.value)))
    final_height = winner[1] if winner is not None else height
    if floor >= final_height:
        issues.append(EvidenceIssue(bid, "min_height", "invalid_min_height",
                                    f"min_height {floor} m is not below height {final_height} m",
                                    _raw(min_height)))
        floor = 0.0
    alternatives = tuple(EvidenceSummary(c.source, h, CONFIDENCE[c.source], d, r, s)
                         for c, h, d, s, r in base[1:])
    decision = HeightDecision(bid, height, floor if floor < height else 0.0, source,
                              CONFIDENCE[source], derivation, reference, text, retrieved,
                              storeys, resolved_class, issues=tuple(issues),
                              alternatives=alternatives)
    if winner is None:
        return decision
    cand, o_height, o_ref = winner
    decision = _override(decision, o_height, o_ref or "", cand.test_only, "reviewed override")
    return decision if decision.min_height_m == floor else replace(decision, min_height_m=floor)


def parse_override_rows(
    rows: Iterable[Mapping[str, str | None]],
) -> tuple[tuple[OverrideRecord, ...], tuple[EvidenceIssue, ...]]:
    """Validate reviewed-override CSV rows (row 1 = header).

    Ids are counted before any other validation: every row whose id occurs more than
    once is rejected with duplicate_override_id, whether or not its siblings are valid.
    """
    materialised = list(rows)
    counts = Counter(r.get("id") for r in materialised if isinstance(r.get("id"), str))
    records: list[OverrideRecord] = []
    issues: list[EvidenceIssue] = []
    src = HeightSource.CITY_SUPPLIED.value
    for row_number, row in enumerate(materialised, start=2):
        raw_id = row.get("id")
        label = raw_id if isinstance(raw_id, str) and raw_id else f"row:{row_number}"
        if isinstance(raw_id, str) and raw_id and counts[raw_id] > 1:
            issues.append(EvidenceIssue(label, src, "duplicate_override_id",
                                        f"id appears {counts[raw_id]} times; all rows rejected",
                                        _raw(row.get("height_m")), row_number))
            continue
        missing = [c for c in OVERRIDE_REQUIRED_COLUMNS if c not in row]
        if missing:
            issues.append(EvidenceIssue(label, src, "missing_column", f"missing {missing}",
                                        "", row_number))
            continue
        try:
            bid = validate_building_id(raw_id)
            if not parse_bool(row["reviewed"]):
                raise EvidenceError("unreviewed_override", "override row is not marked reviewed")
            records.append(OverrideRecord(
                bid, parse_length_m(row["height_m"], row.get("unit")),
                _reference(row["evidence_reference"], required=True) or "",
                parse_bool(row["test_only"]), row_number,
                _reference(row.get("reviewed_by"), required=False)))
        except EvidenceError as exc:
            issues.append(_issue(label, src, exc, dict(row), row_number))
    return tuple(records), tuple(issues)


def apply_overrides(
    decisions: Mapping[str, HeightDecision], records: Iterable[OverrideRecord],
) -> tuple[dict[str, HeightDecision], tuple[EvidenceIssue, ...]]:
    """Apply overrides; unmatched ids and invalid applications are reported, never dropped."""
    result = dict(decisions)
    issues: list[EvidenceIssue] = []
    src = HeightSource.CITY_SUPPLIED.value
    for record in records:
        current = result.get(record.building_id)
        if current is None:
            issues.append(EvidenceIssue(record.building_id, src, "unmatched_override_id",
                                        "no massing feature has this id",
                                        _raw(record.height_m), record.row))
            continue
        try:
            result[record.building_id] = _override(
                current, record.height_m, record.evidence_reference, record.test_only,
                f"reviewed override (row {record.row})")
        except EvidenceError as exc:
            issues.append(_issue(record.building_id, src, exc, record.height_m, record.row))
    return result, tuple(issues)
