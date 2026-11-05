"""utils.parse_json edge cases.

LLM 응답에서 자주 나오는 패턴(코드펜스, harmony 마커, 부분 JSON)을 안정적으로
파싱하는지 검증한다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from pipelines.utils import parse_json  # noqa: E402


class ParseJsonTests(unittest.TestCase):
    def test_plain_json_object(self):
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})

    def test_with_code_fence(self):
        wrapped = '```json\n{"a": 1, "b": [2, 3]}\n```'
        self.assertEqual(parse_json(wrapped), {"a": 1, "b": [2, 3]})

    def test_with_unlabelled_code_fence(self):
        self.assertEqual(parse_json('```\n{"x": "y"}\n```'), {"x": "y"})

    def test_harmony_markers_stripped(self):
        # gpt-oss harmony 채널 마커가 섞여 있어도 파싱
        s = '<|channel|>final<|message|>{"ok": true}<|return|>'
        self.assertEqual(parse_json(s), {"ok": True})

    def test_partial_json_with_trailing_noise(self):
        # 모델이 JSON 뒤에 추가 텍스트를 붙이는 경우
        s = '여기 JSON 입니다: {"k": "v"} 끝.'
        self.assertEqual(parse_json(s), {"k": "v"})

    def test_invalid_escape_corrected(self):
        # \' 는 표준 JSON 에서 invalid 지만 모델이 자주 출력 → 자동 교정
        self.assertEqual(parse_json("{\"name\": \"O\\'Reilly\"}"), {"name": "O'Reilly"})

    def test_top_level_array_rejected(self):
        with self.assertRaises(ValueError):
            parse_json("[1, 2, 3]")

    def test_no_json_object_raises(self):
        with self.assertRaises(ValueError):
            parse_json("이건 그냥 평문이에요")


if __name__ == "__main__":
    unittest.main()
