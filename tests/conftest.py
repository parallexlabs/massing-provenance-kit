"""Synthetic, offline fixtures; network blocking remains in pyproject.toml."""

import csv
import json
from pathlib import Path

import pytest

from massing.heights import load_typology_defaults, resolve_height
from massing.labels import load_label_rules


@pytest.fixture
def fixture_dir():
    return Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def load_json(fixture_dir):
    def load(name):
        with (fixture_dir / name).open(encoding="utf-8") as stream:
            return json.load(stream)
    return load


@pytest.fixture
def load_csv(fixture_dir):
    def load(name):
        with (fixture_dir / name).open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    return load


@pytest.fixture
def footprints_sample(load_json):
    return load_json("footprints_sample.geojson")


@pytest.fixture
def sites_sample(load_json):
    return load_json("sites_sample.geojson")


@pytest.fixture
def osm_sample(load_json):
    return load_json("osm_sample.json")


@pytest.fixture
def override_rows(load_csv):
    return load_csv("overrides_sample.csv")


@pytest.fixture
def typology():
    return load_typology_defaults()


@pytest.fixture
def label_rules():
    return load_label_rules()


@pytest.fixture
def decide(typology):
    def resolve(candidates=(), *, building_id="b1", **kwargs):
        options = {"footprint_area_m2": 100.0, "use_class": None, "typology": typology}
        options.update(kwargs)
        return resolve_height(building_id, candidates, **options)
    return resolve
