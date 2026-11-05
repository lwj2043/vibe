"""Config loading for the vLLM/OpenAI-compatible model pipeline.

Precedence: environment variables > config.json > defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
_ORCH_ROOT = Path(__file__).resolve().parent.parent  # orchestrator/
_DATA_DIR = _ORCH_ROOT.parent / "data"               # vibe-rebuild/data/
SPECS_ROOT = _DATA_DIR / "specs"
SPECS_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "api_key": ("VIBE_API_KEY", "API_KEY"),
    "coder_api_key": ("VIBE_CODER_API_KEY",),
    "vllm_base_url": ("VIBE_VLLM_BASE_URL",),
    "openai_base_url": ("VIBE_OPENAI_BASE_URL", "OPENAI_BASE_URL"),
    "base_url": ("VIBE_BASE_URL",),
    "coder_base_url": ("VIBE_CODER_BASE_URL",),
    "model": ("VIBE_MODEL",),
    "plan_model": ("VIBE_PLAN_MODEL",),
    "dev_model": ("VIBE_DEV_MODEL",),
    "coder_model": ("VIBE_CODER_MODEL",),
}


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return config if isinstance(config, dict) else {}


CONFIG = load_config()


def _env_value(key: str) -> str | None:
    for env_name in _ENV_OVERRIDES.get(key, ()):
        val = os.environ.get(env_name)
        if val:
            return val
    return None


def config_value(*keys: str, default: Any = "") -> Any:
    for key in keys:
        env = _env_value(key)
        if env is not None:
            return env
        if key in CONFIG:
            return CONFIG[key]
    return default
