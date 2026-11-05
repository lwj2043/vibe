"""pipelines.config — 환경변수가 config.json 보다 우선하는지 검증."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from pipelines import config as cfg  # noqa: E402


class ConfigEnvOverrideTests(unittest.TestCase):
    def test_env_var_overrides_config_json(self):
        with mock.patch.dict(cfg.CONFIG, {"api_key": "from-config"}, clear=False), \
             mock.patch.dict(os.environ, {"VIBE_API_KEY": "from-env"}, clear=False):
            self.assertEqual(cfg.config_value("api_key"), "from-env")

    def test_env_alias_api_key(self):
        # VIBE_API_KEY 가 없으면 API_KEY 폴백
        with mock.patch.dict(os.environ, {"API_KEY": "alias"}, clear=False), \
             mock.patch.dict(cfg.CONFIG, {}, clear=True):
            os.environ.pop("VIBE_API_KEY", None)
            self.assertEqual(cfg.config_value("api_key"), "alias")

    def test_falls_back_to_config_json(self):
        with mock.patch.dict(cfg.CONFIG, {"model": "gemma-from-json"}, clear=False), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIBE_MODEL", None)
            self.assertEqual(cfg.config_value("model"), "gemma-from-json")

    def test_default_when_neither_set(self):
        with mock.patch.dict(cfg.CONFIG, {}, clear=True), \
             mock.patch.dict(os.environ, {}, clear=False):
            for env in ("VIBE_MODEL",):
                os.environ.pop(env, None)
            self.assertEqual(cfg.config_value("model", default="fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
