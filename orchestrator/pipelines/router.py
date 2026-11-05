"""Request classification with score-based routing and LLM fallback support."""

from __future__ import annotations

import re
from typing import Any

from .external_data import needs_external_data

VALID_INTENTS = ("chat", "simple_code", "full_spec_code")
VALID_CONFIDENCE = ("high", "low")

IMPLEMENTATION_THRESHOLD = 2
COMPLEXITY_THRESHOLD = 2
CHAT_IMPLEMENTATION_MAX = IMPLEMENTATION_THRESHOLD - 1
SIMPLE_COMPLEXITY_MAX = COMPLEXITY_THRESHOLD - 1

FILE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]+\.(?:py|js|jsx|ts|tsx|html|css|json|md|yml|yaml|toml|go|rs|java|kt|swift|dart)(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)

BUILD_ACTION_PATTERNS = (
    "make",
    "create",
    "build",
    "implement",
    "write",
    "generate",
    "code",
    "develop",
    "scaffold",
    "만들",
    "구현",
    "작성",
    "생성",
    "개발",
    "추가",
    "짜줘",
)

EDIT_ACTION_PATTERNS = (
    "fix",
    "debug",
    "refactor",
    "modify",
    "change",
    "update",
    "repair",
    "patch",
    "고쳐",
    "수정",
    "리팩터",
    "리팩토",
    "반영",
    "적용",
    "바꿔",
    "변경",
)

QUESTION_PATTERNS = (
    "what is",
    "what's",
    "how do",
    "how to",
    "how does",
    "why",
    "difference",
    "explain",
    "meaning",
    "vs",
    "뭐야",
    "무엇",
    "어떻게",
    "왜",
    "차이",
    "설명",
    "알려줘",
    "가능해",
    "추천",
    "방법",
    "원리",
    "정의",
    "어떻게 생각",
)

TECH_KEYWORDS = (
    "html",
    "css",
    "javascript",
    "js",
    "jsx",
    "tsx",
    "react",
    "vue",
    "svelte",
    "next.js",
    "nextjs",
    "node",
    "express",
    "fastapi",
    "flask",
    "django",
    "python",
    "typescript",
    "flutter",
    "dart",
    "api",
    "endpoint",
    "sql",
    "database",
    "db",
    "dom",
    "component",
    "컴포넌트",
    "페이지",
    "웹",
    "사이트",
    "대시보드",
    "로그인",
    "서버",
    "백엔드",
)

SINGLE_ARTIFACT_PATTERNS = (
    "function",
    "component",
    "hook",
    "class",
    "module",
    "script",
    "snippet",
    "endpoint",
    "page",
    "함수",
    "컴포넌트",
    "훅",
    "클래스",
    "모듈",
    "스크립트",
    "스니펫",
    "엔드포인트",
    "페이지",
)

PROJECT_SCOPE_PATTERNS = (
    "app",
    "application",
    "site",
    "website",
    "dashboard",
    "admin",
    "admin panel",
    "game",
    "tool",
    "platform",
    "service",
    "project",
    "mvp",
    "앱",
    "사이트",
    "웹사이트",
    "대시보드",
    "관리자",
    "관리자 페이지",
    "게임",
    "도구",
    "서비스",
    "플랫폼",
    "프로젝트",
)

ARCHITECTURE_PATTERNS = (
    "login",
    "signup",
    "auth",
    "authentication",
    "database",
    "db",
    "api",
    "server",
    "backend",
    "routing",
    "route",
    "deploy",
    "docker",
    "websocket",
    "state management",
    "redux",
    "session",
    "로그인",
    "회원가입",
    "인증",
    "데이터베이스",
    "서버",
    "백엔드",
    "라우팅",
    "배포",
    "상태관리",
    "세션",
)

MULTI_PART_PATTERNS = (
    "multiple files",
    "multi file",
    "file structure",
    "pages",
    "screens",
    "components",
    "multi step",
    "여러 파일",
    "파일 구조",
    "여러 화면",
    "여러 페이지",
    "여러 컴포넌트",
    "여러 단계",
)

FLOW_PATTERNS = (
    "save",
    "search",
    "filter",
    "sort",
    "permission",
    "role",
    "crud",
    "upload",
    "download",
    "delete",
    "edit",
    "persist",
    "저장",
    "검색",
    "필터",
    "정렬",
    "권한",
    "삭제",
    "수정",
    "등록",
    "업로드",
    "다운로드",
    "별점",
    "목록",
)

FRAMEWORK_PATTERNS = (
    "react",
    "next",
    "next.js",
    "vue",
    "svelte",
    "fastapi",
    "django",
    "flask",
    "express",
    "flutter",
)

FULL_SCOPE_PATTERNS = (
    "fullstack",
    "full-stack",
    "end to end",
    "complete",
    "entire",
    "production",
    "전체",
    "완성",
    "완전한",
    "프로덕션",
    "끝까지",
)

SOFT_SMALL_SCOPE_PATTERNS = (
    "simple",
    "quick",
    "short",
    "small",
    "minimal",
    "간단",
    "짧게",
    "작게",
    "미니",
)

HARD_SMALL_SCOPE_PATTERNS = (
    "one function",
    "single function",
    "one component",
    "single component",
    "one file",
    "single file",
    "one page",
    "single page",
    "one screen",
    "single screen",
    "one endpoint",
    "single endpoint",
    "snippet",
    "just the",
    "only the",
    "함수 하나",
    "컴포넌트 하나",
    "페이지 하나",
    "화면 하나",
    "엔드포인트 하나",
    "파일 하나",
    "한 파일",
    "한 파일로",
    "단일 파일",
    "스니펫",
    "이 함수",
    "이 파일",
    "이 코드",
)

LOCAL_CHANGE_PATTERNS = (
    "button",
    "color",
    "text",
    "copy",
    "label",
    "padding",
    "margin",
    "font",
    "css",
    "style",
    "버튼",
    "색",
    "색상",
    "문구",
    "텍스트",
    "라벨",
    "여백",
    "폰트",
    "스타일",
)

EXAMPLE_OR_GUIDE_PATTERNS = (
    "example",
    "sample",
    "template",
    "guide",
    "예시",
    "샘플",
    "템플릿",
    "가이드",
)

DIAGNOSTIC_QUESTION_PATTERNS = (
    "why error",
    "why failing",
    "why fail",
    "what error",
    "error reason",
    "왜 에러",
    "에러 왜",
    "왜 실패",
    "실패 이유",
    "에러 이유",
    "원인",
)


def _is_latin_pattern(pattern: str) -> bool:
    return pattern.isascii() and bool(re.fullmatch(r"[a-z0-9_.+#-]+", pattern))


def _has_pattern(message: str, pattern: str) -> bool:
    pattern = pattern.lower().strip()
    if not pattern:
        return False
    if _is_latin_pattern(pattern):
        return re.search(rf"(?<![a-z0-9_]){re.escape(pattern)}(?![a-z0-9_])", message) is not None
    return pattern in message


def _contains_any(message: str, patterns: tuple[str, ...]) -> bool:
    return any(_has_pattern(message, pattern) for pattern in patterns)


def _hit_count(message: str, patterns: tuple[str, ...]) -> int:
    return sum(1 for pattern in patterns if _has_pattern(message, pattern))


def _has_code_block(user_message: str) -> bool:
    return "```" in user_message


def _has_file_name(user_message: str) -> bool:
    return bool(FILE_PATTERN.search(user_message))


def _condition_count(user_message: str) -> int:
    separators = (
        " and ",
        " with ",
        " plus ",
        " also ",
        ",",
        "\n",
        "그리고",
        "또",
        "및",
        "와 ",
        "과 ",
    )
    return sum(user_message.count(separator) for separator in separators)


def _signals(user_message: str) -> dict[str, Any]:
    raw_message = user_message.lower()
    message = FILE_PATTERN.sub(" ", raw_message)
    flow_hits = _hit_count(message, FLOW_PATTERNS)
    tech_hits = _hit_count(message, TECH_KEYWORDS)
    return {
        "message": message,
        "has_build_action": _contains_any(message, BUILD_ACTION_PATTERNS),
        "has_edit_action": _contains_any(message, EDIT_ACTION_PATTERNS),
        "has_question": user_message.strip().endswith("?") or _contains_any(message, QUESTION_PATTERNS),
        "has_diagnostic_question": _contains_any(message, DIAGNOSTIC_QUESTION_PATTERNS),
        "has_advice_wording": _contains_any(
            message,
            ("가능해", "추천", "어떻게 생각", "can i", "should i"),
        ),
        "has_code_block": _has_code_block(user_message),
        "has_file_name": _has_file_name(user_message),
        "has_single_artifact": _contains_any(message, SINGLE_ARTIFACT_PATTERNS),
        "has_project_scope": _contains_any(message, PROJECT_SCOPE_PATTERNS),
        "has_architecture": _contains_any(message, ARCHITECTURE_PATTERNS),
        "has_multi_part": _contains_any(message, MULTI_PART_PATTERNS),
        "has_framework": _contains_any(message, FRAMEWORK_PATTERNS),
        "has_full_scope": _contains_any(message, FULL_SCOPE_PATTERNS),
        "has_soft_small_scope": _contains_any(message, SOFT_SMALL_SCOPE_PATTERNS),
        "has_hard_small_scope": _contains_any(message, HARD_SMALL_SCOPE_PATTERNS),
        "has_local_change": _contains_any(message, LOCAL_CHANGE_PATTERNS),
        "has_example_or_guide": _contains_any(message, EXAMPLE_OR_GUIDE_PATTERNS),
        "flow_hits": flow_hits,
        "tech_hits": tech_hits,
        "condition_count": _condition_count(user_message),
        "length": len(user_message),
    }


def implementation_score(user_message: str) -> tuple[int, list[str]]:
    """Score whether the user is asking for implementation work."""
    if not user_message or not user_message.strip():
        return 0, ["empty message"]

    sig = _signals(user_message)
    score = 0
    reasons: list[str] = []

    if sig["has_build_action"]:
        score += 3
        reasons.append("+3 build action")
    if sig["has_edit_action"]:
        score += 3
        reasons.append("+3 edit/debug action")
    if sig["has_code_block"]:
        score += 2
        reasons.append("+2 code block")
    if sig["has_file_name"]:
        score += 2
        reasons.append("+2 file name")
    if sig["has_single_artifact"] and (sig["has_build_action"] or sig["has_edit_action"]):
        score += 1
        reasons.append("+1 explicit code artifact")

    tech_hits = min(2, sig["tech_hits"])
    if tech_hits:
        score += tech_hits
        reasons.append(f"+{tech_hits} tech keyword(s)")

    if sig["has_question"] and not (
        sig["has_build_action"]
        or sig["has_edit_action"]
        or sig["has_code_block"]
        or sig["has_file_name"]
    ):
        score -= 4
        reasons.append("-4 pure explanation/question")
    elif sig["has_question"]:
        score -= 1
        reasons.append("-1 question wording on implementation request")

    if sig["has_advice_wording"] and not (sig["has_build_action"] or sig["has_edit_action"]):
        score -= 2
        reasons.append("-2 advice/feasibility wording")
    if sig["has_diagnostic_question"] and not (sig["has_build_action"] or sig["has_edit_action"]):
        score -= 2
        reasons.append("-2 diagnostic question without fix request")

    return score, reasons


def complexity_score(user_message: str) -> tuple[int, list[str]]:
    """Score whether implementation should use the spec -> code pipeline."""
    if not user_message or not user_message.strip():
        return 0, ["empty message"]

    sig = _signals(user_message)
    score = 0
    reasons: list[str] = []

    if sig["has_project_scope"]:
        score += 3
        reasons.append("+3 project/app scope")
    if sig["has_architecture"]:
        score += 3
        reasons.append("+3 architecture feature")
    if sig["has_full_scope"]:
        score += 2
        reasons.append("+2 full-scope wording")
    if sig["has_multi_part"]:
        score += 2
        reasons.append("+2 multi-screen/file/component scope")
    if sig["flow_hits"] >= 3:
        score += 3
        reasons.append("+3 many user/data flows")
    elif sig["flow_hits"] >= 2:
        score += 2
        reasons.append("+2 multiple user/data flows")
    elif sig["flow_hits"] == 1 and sig["has_project_scope"]:
        score += 1
        reasons.append("+1 project with one user/data flow")
    if sig["has_framework"] and (
        sig["has_project_scope"]
        or sig["has_architecture"]
        or sig["has_multi_part"]
        or sig["has_full_scope"]
    ):
        score += 1
        reasons.append("+1 framework in project context")
    if sig["length"] >= 100 or sig["condition_count"] >= 3:
        score += 1
        reasons.append("+1 long or multi-condition request")

    if sig["has_hard_small_scope"]:
        score -= 4
        reasons.append("-4 explicit single-artifact/single-file scope")
    elif sig["has_soft_small_scope"]:
        score -= 2
        reasons.append("-2 soft small-scope wording")
    if (sig["has_code_block"] or sig["has_file_name"]) and sig["has_edit_action"]:
        score -= 3
        reasons.append("-3 existing code/file edit")
    elif sig["has_code_block"] or sig["has_file_name"]:
        score -= 2
        reasons.append("-2 existing code/file context")
    if sig["has_single_artifact"] and not sig["has_project_scope"]:
        score -= 2
        reasons.append("-2 single code artifact")
    if sig["has_local_change"] and not sig["has_project_scope"]:
        score -= 3
        reasons.append("-3 local UI/text/style change")
    if sig["has_example_or_guide"] and not sig["has_project_scope"]:
        score -= 2
        reasons.append("-2 example/guide/template scope")

    return score, reasons


def route_from_scores(implementation_intent_score: int, complexity_score_value: int) -> str:
    """Return the base route from the two public routing scores."""
    if implementation_intent_score <= CHAT_IMPLEMENTATION_MAX:
        return "chat"
    if complexity_score_value <= SIMPLE_COMPLEXITY_MAX:
        return "simple_code"
    return "full_spec_code"


def _choose_intent(
    user_message: str,
    impl_score: int,
    comp_score: int,
) -> tuple[str, str, str]:
    sig = _signals(user_message)
    base_intent = route_from_scores(impl_score, comp_score)
    if sig["has_diagnostic_question"] and not (sig["has_build_action"] or sig["has_edit_action"]):
        return "chat", "diagnostic question without fix request", "high"

    if base_intent == "chat":
        return "chat", "no implementation intent", "high" if impl_score <= 0 else "low"

    explicit_simple = (
        sig["has_hard_small_scope"]
        or ((sig["has_code_block"] or sig["has_file_name"]) and sig["has_edit_action"])
        or (sig["has_local_change"] and not sig["has_project_scope"])
        or (sig["has_single_artifact"] and not sig["has_project_scope"] and not sig["has_architecture"])
    )
    explicit_full = (
        sig["has_full_scope"]
        or (sig["has_project_scope"] and sig["has_architecture"])
        or (sig["has_project_scope"] and sig["has_multi_part"])
        or (sig["has_project_scope"] and sig["flow_hits"] >= 2)
        or (sig["has_project_scope"] and sig["has_build_action"] and not sig["has_soft_small_scope"])
    )

    if explicit_simple and not explicit_full:
        return "simple_code", "explicit small/local/code-context task", "high"
    if explicit_full and not explicit_simple:
        return "full_spec_code", "project scope needs spec/code pipeline", "high"

    if explicit_simple and explicit_full:
        if sig["has_hard_small_scope"] or ((sig["has_code_block"] or sig["has_file_name"]) and sig["has_edit_action"]):
            return "simple_code", "small scope overrides broad wording", "high"
        return "full_spec_code", "small wording conflicts with project scope", "low"

    if base_intent == "full_spec_code":
        confidence = "low" if comp_score <= COMPLEXITY_THRESHOLD else "high"
        return "full_spec_code", "complexity score threshold", confidence

    confidence = "low" if comp_score == COMPLEXITY_THRESHOLD - 1 else "high"
    return "simple_code", "below complexity threshold", confidence


def score_route(user_message: str) -> dict[str, Any]:
    """Classify a request using deterministic implementation/complexity scores."""
    if not user_message or not user_message.strip():
        return {
            "intent": "chat",
            "complexity": "simple",
            "needs_external_data": False,
            "confidence": "high",
            "implementation_score": 0,
            "implementation_intent_score": 0,
            "complexity_score": 0,
            "implementation_reasons": ["empty message"],
            "complexity_reasons": ["empty message"],
            "decision_reason": "empty message",
            "base_intent": "chat",
        }

    impl_score, impl_reasons = implementation_score(user_message)
    comp_score, comp_reasons = complexity_score(user_message)
    intent, decision_reason, confidence = _choose_intent(
        user_message,
        impl_score,
        comp_score,
    )

    if impl_score in (IMPLEMENTATION_THRESHOLD - 1, IMPLEMENTATION_THRESHOLD):
        confidence = "low"

    complexity = "complex" if intent == "full_spec_code" or comp_score >= COMPLEXITY_THRESHOLD else "simple"
    return {
        "intent": intent,
        "complexity": complexity,
        "needs_external_data": needs_external_data(user_message),
        "confidence": confidence,
        "implementation_score": impl_score,
        "implementation_intent_score": impl_score,
        "complexity_score": comp_score,
        "implementation_reasons": impl_reasons,
        "complexity_reasons": comp_reasons,
        "decision_reason": decision_reason,
        "base_intent": route_from_scores(impl_score, comp_score),
    }


def is_simple_question(user_message: str) -> bool:
    """Return True for explanation/advice requests that should stay in chat."""
    if not user_message:
        return False
    route = score_route(user_message)
    return route["intent"] == "chat" and _signals(user_message)["has_question"]


def is_coding_request(user_message: str) -> bool:
    """Return True when the scoring router sees implementation intent."""
    return score_route(user_message)["intent"] != "chat"


def estimate_complexity(user_message: str) -> str:
    """Return 'simple' or 'complex' using the complexity score."""
    return score_route(user_message)["complexity"]


def keyword_fallback_route(user_message: str) -> dict[str, Any]:
    """Compatibility wrapper for callers that expect the old fallback function."""
    route = score_route(user_message)
    route["source"] = "score"
    return route


def normalize_route(raw: dict[str, Any], user_message: str) -> dict[str, Any]:
    scored = score_route(user_message)

    intent = raw.get("intent")
    if intent not in VALID_INTENTS:
        intent = scored["intent"]

    complexity = raw.get("complexity")
    if complexity not in ("simple", "complex"):
        complexity = scored["complexity"]

    needs = raw.get("needs_external_data")
    if not isinstance(needs, bool):
        needs = scored["needs_external_data"]

    confidence = raw.get("confidence")
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"

    return {
        "intent": intent,
        "complexity": complexity,
        "needs_external_data": needs,
        "confidence": confidence,
        "implementation_score": scored["implementation_score"],
        "implementation_intent_score": scored["implementation_intent_score"],
        "complexity_score": scored["complexity_score"],
        "implementation_reasons": scored["implementation_reasons"],
        "complexity_reasons": scored["complexity_reasons"],
        "decision_reason": scored["decision_reason"],
        "base_intent": scored["base_intent"],
    }
