"""router 분류기 — 키워드 기반 정확도 회귀 테스트.

LLM 호출 없이 순수 함수만 검증한다. 향후 분류기 변경 시 회귀를 잡기 위함.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from pipelines.router import (  # noqa: E402
    estimate_complexity,
    is_coding_request,
    is_simple_question,
    keyword_fallback_route,
    normalize_route,
)


class IsSimpleQuestionTests(unittest.TestCase):
    def test_explanation_question(self):
        self.assertTrue(is_simple_question("React가 뭐야?"))

    def test_short_curiosity(self):
        self.assertTrue(is_simple_question("Promise 설명"))

    def test_with_action_verb_is_not_simple(self):
        # "만들어"가 들어가면 코딩 요청으로 본다
        self.assertFalse(is_simple_question("React 컴포넌트 만들어줘"))

    def test_code_fence_is_not_simple(self):
        self.assertFalse(is_simple_question("이 코드 봐줘 ```python\nprint(1)\n```"))

    def test_empty_returns_false(self):
        self.assertFalse(is_simple_question(""))


class IsCodingRequestTests(unittest.TestCase):
    def test_korean_make(self):
        self.assertTrue(is_coding_request("로그인 페이지 만들어줘"))

    def test_english_create(self):
        self.assertTrue(is_coding_request("create a todo app in React"))

    def test_explanation_is_not_coding(self):
        self.assertFalse(is_coding_request("React가 뭐야?"))

    def test_code_fence_triggers_coding(self):
        self.assertTrue(is_coding_request("이거 고쳐줘 ```js\nconst x = 1;\n```"))

    def test_two_tech_keywords_triggers_coding(self):
        # 60자 이상 + 액션 동사 없음 + 기술 키워드 2개 이상 → 코딩 요청
        msg = (
            "this project needs a react component for displaying data, plus an "
            "api endpoint to fetch state from the backend"
        )
        self.assertTrue(is_coding_request(msg))

    def test_empty_returns_false(self):
        self.assertFalse(is_coding_request(""))


class EstimateComplexityTests(unittest.TestCase):
    def test_simple_short(self):
        self.assertEqual(estimate_complexity("간단한 카운터 만들어"), "simple")

    def test_complex_by_keywords(self):
        msg = "DB와 인증, 백엔드 API가 있는 대시보드"
        self.assertEqual(estimate_complexity(msg), "complex")

    def test_complex_by_length(self):
        msg = "어쩌고 " * 90
        self.assertEqual(estimate_complexity(msg), "complex")


class KeywordFallbackRouteTests(unittest.TestCase):
    def test_chat_intent_for_question(self):
        route = keyword_fallback_route("React가 뭐야?")
        self.assertEqual(route["intent"], "chat")

    def test_simple_code_for_short_request(self):
        route = keyword_fallback_route("간단한 함수 만들어줘")
        self.assertEqual(route["intent"], "simple_code")

    def test_full_spec_for_long_request(self):
        msg = "DB 인증 백엔드 API 가 있는 대시보드 풀스택 앱 만들어줘 " * 5
        route = keyword_fallback_route(msg)
        self.assertEqual(route["intent"], "full_spec_code")


class NormalizeRouteTests(unittest.TestCase):
    def test_unknown_intent_falls_back_to_chat(self):
        out = normalize_route({"intent": "garbage"}, "안녕")
        self.assertEqual(out["intent"], "chat")

    def test_missing_confidence_treated_as_high(self):
        out = normalize_route(
            {"intent": "chat", "complexity": "simple"}, "hi"
        )
        self.assertEqual(out["confidence"], "high")

    def test_invalid_complexity_defaults_simple(self):
        out = normalize_route({"intent": "chat", "complexity": "weird"}, "hi")
        self.assertEqual(out["complexity"], "simple")

    def test_non_bool_needs_external_normalized(self):
        out = normalize_route(
            {"intent": "chat", "needs_external_data": "yes"}, "hi"
        )
        self.assertIsInstance(out["needs_external_data"], bool)


if __name__ == "__main__":
    unittest.main()
