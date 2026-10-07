"""massing-provenance-kit: public-evidence LOD1 massing with per-building provenance."""

from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

__version__ = "0.1.0"
__all__ = [
    "CONFIG_FILES",
    "ConfigError",
    "MassingError",
    "__version__",
    "load_yaml_mapping",
    "read_config_text",
]

PROJECT_NAME = "massing-provenance-kit"
CONFIG_FILES = frozenset({"typology_defaults.yaml", "label_rules.yaml"})


class MassingError(Exception):
    """Base class for toolkit errors."""


class ConfigError(MassingError, ValueError):
    """Configuration missing, unreadable or invalid."""


def _source_checkout_config(name: str) -> Path | None:
    root = Path(__file__).resolve().parent.parent
    try:
        meta = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = meta.get("project")
    if not isinstance(project, dict) or project.get("name") != PROJECT_NAME:
        return None
    candidate = root / "config" / name
    return candidate if candidate.is_file() else None


def read_config_text(name: str, path: Path | None = None) -> tuple[str, str]:
    """Return (text, origin). Never consults the current working directory.

    Order: explicit ``path``; packaged ``massing/config/<name>`` (installed wheel);
    ``config/<name>`` of a verified source checkout (editable install).
    """
    if path is not None:
        try:
            return path.read_text(encoding="utf-8"), f"file:{path.resolve()}"
        except OSError as exc:
            raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if name not in CONFIG_FILES:
        raise ConfigError(f"unknown config file {name!r}; expected one of {sorted(CONFIG_FILES)}")
    packaged = resources.files("massing") / "config" / name
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8"), f"package:massing/config/{name}"
    checkout = _source_checkout_config(name)
    if checkout is not None:
        return checkout.read_text(encoding="utf-8"), f"checkout:{checkout}"
    raise ConfigError(f"packaged config {name!r} not found; reinstall massing-provenance-kit")


def load_yaml_mapping(name: str, path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Safely load a YAML mapping config; returns (data, origin)."""
    text, origin = read_config_text(name, path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {origin}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{origin} must contain a YAML mapping at top level")
    if data.get("version") != 1:
        raise ConfigError(f"{origin}: unsupported config version {data.get('version')!r}")
    return data, origin
