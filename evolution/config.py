from __future__ import annotations

import importlib
import os
import re
from pathlib import Path
from typing import Any

import yaml


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_config(path: Path) -> dict[str, Any]:
    config_path = path.resolve()
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("evolution config must be a YAML mapping")
    expanded = _expand(payload)
    expanded["_config_dir"] = str(config_path.parent)
    return expanded


def import_symbol(import_path: str):
    if ":" not in import_path:
        raise ValueError("import_path must use module:attribute form")
    module_name, attribute = import_path.split(":", 1)
    return getattr(importlib.import_module(module_name), attribute)


def resolve_path(config: dict[str, Any], value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (Path(config["_config_dir"]) / path).resolve()


def _expand(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if not isinstance(value, str):
        return value

    def replacement(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"required environment variable is not set: {name}")
        return os.environ[name]

    return _ENV_RE.sub(replacement, value)
