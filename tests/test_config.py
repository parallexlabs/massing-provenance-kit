from copy import deepcopy

import pytest
import yaml

from massing import ConfigError, load_yaml_mapping, read_config_text
from massing.heights import load_typology_defaults, typology_from_mapping
from massing.labels import label_rules_from_mapping, load_label_rules


def test_defaults_ignore_cwd_and_shadow_configs(tmp_path, monkeypatch):
    expected_typology = load_typology_defaults()
    expected_labels = load_label_rules()
    shadow = tmp_path / "config"
    shadow.mkdir()
    for directory in (tmp_path, shadow):
        for name in ("typology_defaults.yaml", "label_rules.yaml"):
            (directory / name).write_text("invalid: [", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_typology_defaults() == expected_typology
    assert load_label_rules() == expected_labels
    for name in ("typology_defaults.yaml", "label_rules.yaml"):
        text, origin = read_config_text(name)
        assert yaml.safe_load(text)["version"] == 1
        assert origin.startswith(("package:", "checkout:"))


def test_explicit_config_path_and_origin(tmp_path, monkeypatch):
    path = tmp_path / "custom.yaml"
    path.write_text(
        "version: 1\nclasses:\n  custom:\n"
        "    - {max_area_m2: null, height_m: 12}\naliases: {demo: custom}\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    configured = load_typology_defaults(path)
    assert configured.origin == f"file:{path.resolve()}"
    assert configured.resolve_use_class(" DEMO ") == "custom"
    assert configured.height_for(100, "custom") == 12


@pytest.mark.parametrize("text", [
    "invalid: [", "[]", "", "version: 2", "classes: {}",
    "version: 1\nx: !!python/object:builtins.object {}",
])
def test_bad_yaml_or_version(tmp_path, text):
    path = tmp_path / "bad.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_yaml_mapping("typology_defaults.yaml", path)


def test_missing_and_unknown_config(tmp_path):
    with pytest.raises(ConfigError):
        read_config_text("typology_defaults.yaml", tmp_path / "absent.yaml")
    with pytest.raises(ConfigError):
        read_config_text("../unapproved.yaml")


@pytest.mark.parametrize("area,use_class,height", [
    (60, "accessory", 3), (60.01, "accessory", 4.6),
    (250, "residential_detached", 7.7), (250.01, "residential_detached", 10.8),
    (1000, "commercial", 6), (1000.01, "commercial", 8),
])
def test_documented_typology_boundaries(typology, area, use_class, height):
    assert typology.height_for(area, use_class) == height


@pytest.mark.parametrize("classes,aliases", [
    ({}, {}), ({"Upper": [{"max_area_m2": None, "height_m": 4}]}, {}),
    ({"x": []}, {}), ({"x": [{"height_m": 4}]}, {}),
    ({"x": [{"max_area_m2": 100, "height_m": 4}]}, {}),
    ({"x": [{"max_area_m2": None, "height_m": 4},
            {"max_area_m2": None, "height_m": 5}]}, {}),
    ({"x": [{"max_area_m2": 100, "height_m": 4},
            {"max_area_m2": 90, "height_m": 5},
            {"max_area_m2": None, "height_m": 6}]}, {}),
    ({"x": [{"max_area_m2": None, "height_m": 0}]}, {}),
    ({"x": [{"max_area_m2": None, "height_m": True}]}, {}),
    ({"x": [{"max_area_m2": None, "height_m": float("nan")}]}, {}),
    ({"x": [{"max_area_m2": None, "height_m": 4}]}, {"demo": "missing"}),
    ({"x": [{"max_area_m2": None, "height_m": 4}]}, {"demo": []}),
])
def test_bad_typology_configuration(classes, aliases):
    with pytest.raises(ConfigError):
        typology_from_mapping({"version": 1, "classes": classes, "aliases": aliases})


@pytest.mark.parametrize("template", [
    "", "{height_m", "{other}", "{height_m!r}", "{height_m.real}",
    "{height_m[0]}", "{height_m:.1q}", "{height_m}\nsecond line", "constant",
])
def test_bad_label_templates(template):
    data, _ = load_yaml_mapping("label_rules.yaml")
    data = deepcopy(data)
    data["rules"]["unknown"]["template"] = template
    with pytest.raises(ConfigError):
        label_rules_from_mapping(data)


@pytest.mark.parametrize("mutation", ["missing", "extra", "entry", "flag", "unhashable"])
def test_bad_label_rule_structure(mutation):
    data, _ = load_yaml_mapping("label_rules.yaml")
    if mutation == "missing":
        del data["rules"]["unknown"]
    elif mutation == "extra":
        data["rules"]["extra"] = data["rules"]["unknown"]
    elif mutation == "entry":
        data["rules"]["unknown"]["extra"] = 1
    else:
        data["rules"]["unknown"]["style_flag"] = [] if mutation == "unhashable" else "bad"
    with pytest.raises(ConfigError):
        label_rules_from_mapping(data)


@pytest.mark.parametrize("key,flag", [
    ("unknown", "evidence"), ("typology_default", "estimated"),
    ("city_supplied_test_only", "evidence"), ("osm_height", "test_only"),
])
def test_required_and_reserved_styles(key, flag):
    data, _ = load_yaml_mapping("label_rules.yaml")
    data["rules"][key]["style_flag"] = flag
    with pytest.raises(ConfigError):
        label_rules_from_mapping(data)
