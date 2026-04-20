"""Config loading for the dual model pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
SPECS_ROOT = Path(__file__).resolve().parent.parent / "specs"


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return config if isinstance(config, dict) else {}


CONFIG = load_config()


def config_value(*keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in CONFIG:
            return CONFIG[key]
    return default
