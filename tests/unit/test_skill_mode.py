"""Skill mode override behavior."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from pipelines.dual_model_pipeline import Pipeline  # noqa: E402


class SkillModeTests(unittest.TestCase):
    def test_auto_keeps_route(self):
        route = {"intent": "chat", "complexity": "simple", "needs_external_data": False}
        self.assertIs(Pipeline._apply_skill_mode(route, "auto"), route)

    def test_chat_forces_chat_route(self):
        route = {"intent": "full_spec_code", "complexity": "complex"}
        out = Pipeline._apply_skill_mode(route, "chat")

        self.assertEqual(out["intent"], "chat")
        self.assertEqual(out["complexity"], "simple")
        self.assertEqual(out["source"], "skill_override")

    def test_code_turns_chat_route_into_coding_route(self):
        route = {"intent": "chat", "complexity": "simple"}
        out = Pipeline._apply_skill_mode(route, "code")

        self.assertEqual(out["intent"], "simple_code")
        self.assertEqual(out["source"], "skill_override")


if __name__ == "__main__":
    unittest.main()
