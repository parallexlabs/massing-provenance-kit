"""Exact approved label wording per evidence source (pure rendering)."""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from massing import ConfigError, load_yaml_mapping
from massing.heights import HeightDecision, HeightSource

TEST_ONLY_KEY = "city_supplied_test_only"
RULE_KEYS: tuple[str, ...] = (*(source.value for source in HeightSource), TEST_ONLY_KEY)
STYLE_FLAGS = frozenset({"evidence", "estimated", "uncertain", "test_only"})
REQUIRED_STYLE: Mapping[str, str] = {
    HeightSource.TYPOLOGY_DEFAULT.value: "uncertain",
    HeightSource.UNKNOWN.value: "uncertain",
    TEST_ONLY_KEY: "test_only",
}
RESERVED_STYLE = "test_only"
_COMMON = frozenset({"height_m", "min_height_m", "confidence"})
_OVERRIDE = _COMMON | {"reference", "height_prev_m", "height_prev_source"}
ALLOWED_FIELDS: Mapping[str, frozenset[str]] = {
    HeightSource.CITY_SUPPLIED.value: _OVERRIDE,
    TEST_ONLY_KEY: _OVERRIDE,
    HeightSource.OSM_HEIGHT.value: _COMMON,
    HeightSource.OSM_LEVELS.value: _COMMON | {"storeys"},
    HeightSource.APPLICATION_STOREYS.value: _COMMON | {"storeys", "reference"},
    HeightSource.TYPOLOGY_DEFAULT.value: _COMMON | {"use_class"},
    HeightSource.UNKNOWN.value: _COMMON,
}
_SAMPLE: Mapping[str, object] = {
    "height_m": 32.5, "min_height_m": 0.0, "confidence": 0.7, "reference": "REF-1",
    "height_prev_m": 4.0, "height_prev_source": "unknown", "storeys": 10,
    "use_class": "commercial",
}
LABELS_CSV_COLUMNS: tuple[str, ...] = (
    "id", "label_text", "style_flag", "label_rule", "height_m", "min_height_m",
    "height_source", "height_confidence", "height_reference", "height_prev_m",
)


@dataclass(frozen=True, slots=True)
class LabelRule:
    key: str
    template: str
    style_flag: str
    fields: frozenset[str]


@dataclass(frozen=True, slots=True)
class Label:
    rule_key: str
    text: str
    style_flag: str


def _template_fields(key: str, template: str, origin: str) -> frozenset[str]:
    if not template.strip() or "\n" in template or "\r" in template:
        raise ConfigError(f"{origin}: rule {key!r} template must be one non-empty line")
    names: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ConfigError(f"{origin}: rule {key!r} template is malformed: {exc}") from exc
    for _, field, _, conversion in parsed:
        if field is None:
            continue
        if conversion is not None or field not in ALLOWED_FIELDS[key]:
            raise ConfigError(f"{origin}: rule {key!r} uses disallowed placeholder {field!r}")
        names.add(field)
    if "height_m" not in names:
        raise ConfigError(f"{origin}: rule {key!r} must show {{height_m}}")
    try:
        template.format(**_SAMPLE)
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfigError(f"{origin}: rule {key!r} format spec invalid: {exc}") from exc
    return frozenset(names)


@dataclass(frozen=True, slots=True)
class LabelRules:
    rules: Mapping[str, LabelRule]
    origin: str = "mapping"

    def rule_for(self, decision: HeightDecision) -> LabelRule:
        return self.rules[decision.label_key]

    def render(self, decision: HeightDecision) -> Label:
        rule = self.rule_for(decision)
        values = label_values(decision)
        missing = sorted(name for name in rule.fields if values.get(name) is None)
        if missing:
            raise ValueError(f"{decision.building_id}: rule {rule.key!r} needs {missing}")
        return Label(rule.key, rule.template.format(**values), rule.style_flag)


def label_values(decision: HeightDecision) -> dict[str, object]:
    prev = decision.height_prev_source
    return {
        "height_m": decision.height_m,
        "min_height_m": decision.min_height_m,
        "confidence": decision.confidence,
        "reference": decision.reference,
        "height_prev_m": decision.height_prev_m,
        "height_prev_source": prev.value if prev is not None else None,
        "storeys": decision.storeys,
        "use_class": decision.use_class,
    }


def label_rules_from_mapping(data: Mapping[str, Any], origin: str = "mapping") -> LabelRules:
    raw = data.get("rules")
    if not isinstance(raw, dict):
        raise ConfigError(f"{origin}: 'rules' must be a mapping")
    if set(raw) != set(RULE_KEYS):
        raise ConfigError(f"{origin}: rules must be exactly {sorted(RULE_KEYS)}; "
                          f"missing {sorted(set(RULE_KEYS) - set(raw))}, "
                          f"extra {sorted(str(k) for k in set(raw) - set(RULE_KEYS))}")
    rules: dict[str, LabelRule] = {}
    for key in RULE_KEYS:
        entry = raw[key]
        if not isinstance(entry, dict) or set(entry) != {"template", "style_flag"}:
            raise ConfigError(f"{origin}: rule {key!r} needs exactly template and style_flag")
        template, flag = entry["template"], entry["style_flag"]
        if not isinstance(template, str):
            raise ConfigError(f"{origin}: rule {key!r} template must be a string")
        if not isinstance(flag, str) or flag not in STYLE_FLAGS:
            raise ConfigError(f"{origin}: rule {key!r} style_flag {flag!r} must be one of "
                              f"{sorted(STYLE_FLAGS)}")
        if key in REQUIRED_STYLE and flag != REQUIRED_STYLE[key]:
            raise ConfigError(f"{origin}: rule {key!r} must use style_flag {REQUIRED_STYLE[key]!r}")
        if key not in REQUIRED_STYLE and flag == RESERVED_STYLE:
            raise ConfigError(f"{origin}: only {TEST_ONLY_KEY!r} may use style_flag 'test_only'")
        rules[key] = LabelRule(key, template, flag, _template_fields(key, template, origin))
    return LabelRules(rules, origin)


def load_label_rules(path: Path | None = None) -> LabelRules:
    data, origin = load_yaml_mapping("label_rules.yaml", path)
    return label_rules_from_mapping(data, origin)


def _num(value: float | None) -> str:
    return "" if value is None else format(value, ".3f")


def label_row(decision: HeightDecision, label: Label) -> dict[str, str]:
    """One labels.csv row; deterministic string formatting (3 dp)."""
    return {
        "id": decision.building_id,
        "label_text": label.text,
        "style_flag": label.style_flag,
        "label_rule": label.rule_key,
        "height_m": _num(decision.height_m),
        "min_height_m": _num(decision.min_height_m),
        "height_source": decision.source.value,
        "height_confidence": format(decision.confidence, ".1f"),
        "height_reference": decision.reference or "",
        "height_prev_m": _num(decision.height_prev_m),
    }
