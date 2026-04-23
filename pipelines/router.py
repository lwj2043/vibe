"""Request classification: LLM router with keyword-based fallback."""

from __future__ import annotations

from typing import Any

from .external_data import needs_external_data

# 액션 동사 — 하나만 있어도 강한 코딩 요청 신호
CODING_ACTION_PATTERNS = (
    "만들어", "만들자", "만들어줘", "만들어봐", "만들래",
    "구현", "짜줘", "짜봐", "짜서", "개발해", "작성해",
    "생성해", "고쳐", "수정해", "리팩터", "리팩토",
    "create ", "build ", "implement ", "write a ", "code a ",
    "refactor", "fix the", "generate a ",
)
# 기술 키워드 — 단독으로도 코딩 요청일 가능성이 높음
CODING_TECH_KEYWORDS = (
    "html", "css", "javascript", "js", "jsx", "tsx", "react", "vue",
    "svelte", "next.js", "nextjs", "node", "express", "fastapi",
    "flask", "django", "python", "typescript", "flutter", "dart",
    "api", "endpoint", "sql", "database", "dom", "component",
    "컴포넌트", "페이지", "웹사이트", "웹페이지", "웹앱", "앱",
    "대시보드", "로그인 화면", "회원가입", "todo 앱", "계산기",
    "스크립트", "함수", "클래스", "모듈",
)

# 단순 질의(설명/개념/비교/방법) — 매칭되면 채팅 경로로
SIMPLE_QUESTION_PATTERNS = (
    "뭐야", "뭐지", "무엇", "어떻게", "왜 ", "왜?", "왜야", "이유",
    "차이", "설명", "알려줘", "궁금", "가능해", "가능한", "되나요", "되니",
    "what is", "what's", "how do", "how to", "how does", "why ",
    "difference", "explain", "vs ", "meaning",
)

# 복잡도 추정 키워드
COMPLEXITY_INDICATORS = (
    "데이터베이스", "DB", "인증", "로그인", "API", "백엔드",
    "서버", "배포", "Docker", "Next.js", "React", "라우팅",
    "상태 관리", "Redux", "database", "authentication",
    "WebSocket", "실시간", "대시보드", "관리자",
)

VALID_INTENTS = ("chat", "simple_code", "full_spec_code")
VALID_CONFIDENCE = ("high", "low")


def is_simple_question(user_message: str) -> bool:
    """코드/기술 관련이어도 '설명/개념 질문'이면 True."""
    if not user_message:
        return False
    msg = user_message.strip().lower()
    for phrase in CODING_ACTION_PATTERNS:
        if phrase in msg:
            return False
    if "```" in user_message:
        return False
    if msg.endswith("?") or msg.endswith("？"):  # 영문/전각 물음표
        return True
    for phrase in SIMPLE_QUESTION_PATTERNS:
        if phrase in msg:
            return True
    if len(msg) < 60 and not any(
        kw in msg for kw in ("만들", "구현", "생성", "작성", "build", "create")
    ):
        return True
    return False


def is_coding_request(user_message: str) -> bool:
    """코딩/구현 요청이면 True."""
    if not user_message:
        return False
    if is_simple_question(user_message):
        return False

    msg = user_message.lower()
    for phrase in CODING_ACTION_PATTERNS:
        if phrase in msg:
            return True
    if "```" in user_message:
        return True
    tech_hits = sum(1 for kw in CODING_TECH_KEYWORDS if kw in msg)
    if tech_hits >= 2:
        return True
    return False


def estimate_complexity(user_message: str) -> str:
    """Return 'simple' or 'complex'."""
    message_lower = user_message.lower()
    hits = sum(1 for ind in COMPLEXITY_INDICATORS if ind.lower() in message_lower)
    word_count = len(user_message.split())
    if hits >= 2 or word_count > 80:
        return "complex"
    return "simple"


def keyword_fallback_route(user_message: str) -> dict[str, Any]:
    """키워드 기반 폴백 라우트."""
    if not is_coding_request(user_message):
        intent = "chat"
    elif "```" in user_message or len(user_message.strip()) < 80:
        intent = "simple_code"
    else:
        intent = "full_spec_code"
    return {
        "intent": intent,
        "complexity": estimate_complexity(user_message),
        "needs_external_data": needs_external_data(user_message),
    }


def normalize_route(raw: dict[str, Any], user_message: str) -> dict[str, Any]:
    intent = raw.get("intent")
    if intent not in VALID_INTENTS:
        intent = "chat"
    complexity = raw.get("complexity")
    if complexity not in ("simple", "complex"):
        complexity = "simple"
    needs = raw.get("needs_external_data")
    if not isinstance(needs, bool):
        needs = needs_external_data(user_message)
    confidence = raw.get("confidence")
    if confidence not in VALID_CONFIDENCE:
        # 필드 누락/오타는 "high" 로 간주 — 폴백을 남용하지 않는다
        confidence = "high"
    return {
        "intent": intent,
        "complexity": complexity,
        "needs_external_data": needs,
        "confidence": confidence,
    }
