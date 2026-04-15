"""Open WebUI pipeline for a two-model vibe-coding workflow.

Dual Model Pipeline v2 - Spec Model + Coder Model separation architecture.
Supports initial generation and iterative modification via diff specs.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Few-shot example for spec model
# ---------------------------------------------------------------------------
FEW_SHOT_SPEC_EXAMPLE = json.dumps(
    {
        "project": {
            "name": "Todo App",
            "type": "static-web",
            "scope": "frontend",
            "tech_stack": ["HTML", "CSS", "Vanilla JS"],
            "description": "로컬 스토리지 기반 할일 관리 앱",
        },
        "files": [
            {"path": "index.html", "role": "메인 진입점, 전체 레이아웃 구조"},
            {"path": "style.css", "role": "다크모드 지원 스타일시트"},
            {"path": "app.js", "role": "CRUD 로직, 로컬스토리지 연동"},
        ],
        "components": [
            {
                "name": "TodoList",
                "props": ["items: Array", "onDelete: Function"],
                "behavior": "항목 클릭 시 완료 토글, 완료 항목 취소선 표시",
            }
        ],
        "api": [],
        "constraints": [
            "외부 라이브러리 사용 금지",
            "모바일 반응형 필수",
            "색상 팔레트: #1a1a2e, #16213e, #e94560",
        ],
        "user_story": "사용자는 할일을 추가·완료·삭제할 수 있다",
    },
    ensure_ascii=False,
    indent=2,
)

FEW_SHOT_DIFF_EXAMPLE = json.dumps(
    {
        "modified_files": [
            {
                "path": "style.css",
                "changes": "버튼 색상을 #e94560에서 #00d2ff로 변경",
            }
        ],
        "new_constraints": ["버튼에 hover 애니메이션 추가"],
        "removed_components": [],
    },
    ensure_ascii=False,
    indent=2,
)

FRONTEND_FRAMEWORK_RULE = """
프론트엔드 프레임워크 선택 규칙 (반드시 아래 3가지 중 하나만 선택):

[방법 1] Vanilla HTML/CSS/JS
- 선택 기준: 단순 정적 페이지, 랜딩 페이지, 학습용 예제, 단일 화면 도구, 작은 유틸리티
- 파일 규칙: **반드시 단일 `.html` 파일 하나**로만 작성합니다.
  * CSS는 `<style>` 태그 안에 인라인으로 작성합니다.
  * JS는 `<script>` 태그 안에 인라인으로 작성합니다.
  * `style.css`, `script.js`, `app.js` 같은 **별도 파일을 절대 만들지 마십시오.**
  * `<link rel="stylesheet" href="...">` 와 `<script src="외부파일.js">` 를 사용하지 마십시오.
  * 외부 CDN 라이브러리(예: CDN 링크)는 허용됩니다.

[방법 2] React
- 선택 기준: 상태 관리가 중요한 SPA, 컴포넌트 재사용이 많은 UI, 동적 라우팅, 복잡한 상호작용, 대시보드
- 파일 규칙: Vite 기반 프로젝트 구조로 작성합니다.
  * `package.json`, `index.html`, `src/main.jsx`, `src/App.jsx`, 필요 시 `src/components/*.jsx`
  * 스타일은 `src/App.css` 또는 CSS Modules 사용 가능
  * TypeScript가 꼭 필요하지 않으면 `.jsx` 사용

[방법 3] Flutter
- 선택 기준: 모바일 앱, 크로스플랫폼 앱, 네이티브 앱 느낌의 UI, "앱 만들어줘" 요청
- 파일 규칙: Flutter 프로젝트 구조로 작성합니다.
  * `pubspec.yaml`, `lib/main.dart`, 필요 시 `lib/screens/*.dart`, `lib/widgets/*.dart`

중요:
- 한 응답 안에서 반드시 **하나의 방법만** 선택합니다. 여러 방법을 섞지 마십시오.
- 사용자가 특정 프레임워크를 명시하면 그것을 따릅니다.
- 단순 요청(한 화면짜리, 계산기, 시계, 할일 앱 등)은 기본적으로 방법 1을 선택합니다.
- 백엔드/CLI/데이터 요청에는 이 규칙이 적용되지 않습니다.
""".strip()

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
SPECS_ROOT = Path(__file__).resolve().parent.parent / "specs"


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return config if isinstance(config, dict) else {}


CONFIG = _load_config()


def _config_value(*keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in CONFIG:
            return CONFIG[key]
    return default


class Pipeline:
    """Dual Model Pipeline with spec/coder separation and diff-based updates."""

    class Valves(BaseModel):
        model_config = ConfigDict(protected_namespaces=())

        spec_model: str = Field(
            default=str(_config_value("plan_model", "spec_model", default="llama3.1:8b")),
            description=(
                "Spec model name. "
                "Ollama: llama3.1:8b"
            ),
        )
        coder_model: str = Field(
            default=str(_config_value("dev_model", "coder_model", default="qwen2.5-coder:7b")),
            description=(
                "Coder model name. "
                "Ollama: qwen2.5-coder:7b"
            ),
        )
        single_pass: bool = Field(
            default=bool(_config_value("single_pass", default=True)),
            description="Use one model call instead of spec+coder calls to reduce API usage.",
        )
        # ── 자동 라우팅용 경량 모델 (선택) ──────────────────────────────
        spec_model_light: str = Field(
            default=str(_config_value("spec_model_light", default="")),
            description="Lightweight spec model for simple requests. Ollama: llama3.2:3b (비워두면 spec_model 사용)",
        )
        coder_model_light: str = Field(
            default=str(_config_value("coder_model_light", default="")),
            description="Lightweight coder model for simple requests. Ollama: qwen2.5-coder:7b (비워두면 coder_model 사용)",
        )
        ollama_url: str = Field(
            default=str(_config_value("ollama_url", default="http://localhost:11434")),
            description="Ollama API base URL. Example: http://192.168.0.10:11434",
        )
        auto_route: bool = Field(
            default=bool(_config_value("auto_route", default=False)),
            description="Auto-select model size by request complexity",
        )

    # ------------------------------------------------------------------
    # System Prompts
    # ------------------------------------------------------------------
    SPEC_SYSTEM_PROMPT = f"""당신은 소프트웨어 명세 작성 전문가입니다.
사용자의 자연어 요청을 분석하여 반드시 아래 JSON 스키마 형식의 기술 명세서만 출력하십시오.
설명 텍스트, Markdown 코드블록, 주석 없이 순수 JSON만 출력합니다.

필수 스키마:
{{
  "project": {{
    "name": "string",
    "type": "string (vanilla-web | react-spa | flutter-app | node-app | cli | data 중 하나)",
    "scope": "string (frontend | backend | fullstack | cli | data | general 중 하나)",
    "tech_stack": ["string"],
    "description": "string"
  }},
  "files": [
    {{ "path": "string", "role": "string (이 파일의 역할 설명)" }}
  ],
  "components": [
    {{
      "name": "string",
      "props": ["string"],
      "behavior": "string"
    }}
  ],
  "api": [],
  "constraints": ["string"],
  "user_story": "string"
}}

scope 규칙:
- 사용자의 요청이 화면/UI/웹페이지 중심이면 "frontend"를 선택합니다.
- API, 서버, DB, 인증, 배치 작업 중심이면 "backend"를 선택합니다.
- 프론트엔드와 백엔드가 모두 필요하면 "fullstack"을 선택합니다.
- 터미널 도구나 스크립트 중심이면 "cli"를 선택합니다.
- 데이터 처리/분석 중심이면 "data"를 선택합니다.
- 어느 쪽에도 명확히 속하지 않으면 "general"을 선택합니다.
- 모든 요청을 프론트엔드로 간주하지 마십시오. scope에 맞는 파일만 설계하십시오.

아래는 올바른 출력 예시입니다:
{FEW_SHOT_SPEC_EXAMPLE}""".strip()

    CODER_SYSTEM_PROMPT = """당신은 코드 생성 전문가입니다.
주어진 기술 명세서 JSON을 100% 준수하여 코드를 생성하십시오.
응답에는 먼저 구현 설명을 3~6문장으로 작성하고, 그 다음 실제 파일별 코드를 생성하십시오.
project.scope가 frontend가 아니라면 프론트엔드 파일을 억지로 만들지 말고, 해당 scope에 필요한 서버/CLI/데이터 처리 파일을 생성하십시오.
각 파일은 반드시 아래 형식으로 출력합니다.

```파일경로
코드내용
```

명세에 없는 기능은 추가하지 말고, 파일 경로는 명세서의 files 배열에 있는 값만 사용하십시오.
코드는 그대로 저장해 실행할 수 있는 완성본이어야 합니다.""".strip()

    SINGLE_PASS_SYSTEM_PROMPT = """당신은 코드 생성 전문가입니다.
사용자 요청을 내부적으로 분석해 작업 범위를 frontend, backend, fullstack, cli, data, general 중 하나로 판단하십시오.
판단 결과와 중간 분석 과정은 절대 출력하지 말고, 최종 구현 설명과 파일별 코드만 출력하십시오.
요청이 프론트엔드가 아니라면 프론트엔드 파일을 억지로 만들지 말고, 해당 범위에 필요한 서버/CLI/데이터 처리 파일을 생성하십시오.

응답에는 먼저 구현 설명을 3~6문장으로 작성하고, 그 다음 실제 파일별 코드를 생성하십시오.
각 파일은 반드시 아래 형식으로 출력합니다.

```파일경로
코드내용
```

코드는 그대로 저장해 실행할 수 있는 완성본이어야 합니다.""".strip()

    DIFF_SPEC_PROMPT = f"""당신은 소프트웨어 명세서 diff 전문가입니다.
원본 명세서와 사용자의 수정 요청을 비교하여, 변경이 필요한 항목만 담은 diff 명세서를 JSON으로 출력하세요.
설명 텍스트, Markdown 코드블록, 주석 없이 순수 JSON만 출력합니다.

필수 형식:
{{
  "modified_files": [
    {{ "path": "string", "changes": "변경 내용 설명" }}
  ],
  "new_constraints": ["새로 추가할 제약 조건"],
  "removed_components": ["삭제할 컴포넌트 이름"]
}}

아래는 올바른 출력 예시입니다:
{FEW_SHOT_DIFF_EXAMPLE}""".strip()

    DIFF_CODER_PROMPT = """당신은 코드 수정 전문가입니다.
기존 코드와 diff 명세서가 주어집니다. diff 명세서에 명시된 변경 사항만 반영하여 수정된 파일을 출력하십시오.
변경되지 않는 파일은 출력하지 마십시오.
각 파일은 반드시 아래 형식으로 출력합니다.

```파일경로
전체코드내용
```

diff에 없는 변경은 절대 하지 마십시오.""".strip()

    SPEC_SYSTEM_PROMPT = f"{SPEC_SYSTEM_PROMPT}\n\n{FRONTEND_FRAMEWORK_RULE}"
    CODER_SYSTEM_PROMPT = f"{CODER_SYSTEM_PROMPT}\n\n{FRONTEND_FRAMEWORK_RULE}"
    SINGLE_PASS_SYSTEM_PROMPT = f"{SINGLE_PASS_SYSTEM_PROMPT}\n\n{FRONTEND_FRAMEWORK_RULE}"
    DIFF_CODER_PROMPT = f"{DIFF_CODER_PROMPT}\n\n{FRONTEND_FRAMEWORK_RULE}"

    def __init__(self) -> None:
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # Async execution helper
    # ------------------------------------------------------------------
    @staticmethod
    def _run_async(coro: Any) -> Any:
        """Run a coroutine safely from a synchronous context.

        Open WebUI may call ``pipe`` from within an already-running event loop.
        In that case ``asyncio.run()`` raises RuntimeError, so we fall back to
        executing the coroutine in a dedicated daemon thread with its own loop.
        """
        try:
            asyncio.get_running_loop()
            # Already inside a running loop — spin up a new thread.
            result_holder: list[Any] = []
            exc_holder: list[BaseException] = []

            def _run() -> None:
                try:
                    result_holder.append(asyncio.run(coro))
                except BaseException as exc:  # noqa: BLE001
                    exc_holder.append(exc)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join()
            if exc_holder:
                raise exc_holder[0]
            return result_holder[0]
        except RuntimeError:
            # No running loop in the current thread.
            return asyncio.run(coro)

    # ------------------------------------------------------------------
    # Coding vs. general-chat classifier
    # ------------------------------------------------------------------
    # 액션 동사 (만들어달라/구현해달라/짜달라 등) — 하나만 있어도 강한 신호
    _CODING_ACTION_PATTERNS = (
        "만들어", "만들자", "만들어줘", "만들어봐", "만들래",
        "구현", "짜줘", "짜봐", "짜서", "개발해", "작성해",
        "생성해", "고쳐", "수정해", "리팩터", "리팩토",
        "create ", "build ", "implement ", "write a ", "code a ",
        "refactor", "fix the", "generate a ",
    )
    # 기술 키워드 — 단독으로도 코딩 요청일 가능성이 높음
    _CODING_TECH_KEYWORDS = (
        "html", "css", "javascript", "js", "jsx", "tsx", "react", "vue",
        "svelte", "next.js", "nextjs", "node", "express", "fastapi",
        "flask", "django", "python", "typescript", "flutter", "dart",
        "api", "endpoint", "sql", "database", "dom", "component",
        "컴포넌트", "페이지", "웹사이트", "웹페이지", "웹앱", "앱",
        "대시보드", "로그인 화면", "회원가입", "todo 앱", "계산기",
        "스크립트", "함수", "클래스", "모듈",
    )

    # 단순 질의(설명/개념/비교/방법 문의) 패턴 — 매칭되면 명세+코드 파이프라인을
    # 건너뛰고 단일 모델(coder)로 바로 답변합니다.
    _SIMPLE_QUESTION_PATTERNS = (
        "뭐야", "뭐지", "무엇", "어떻게", "왜 ", "왜?", "왜야", "이유",
        "차이", "설명", "알려줘", "궁금", "가능해", "가능한", "되나요", "되니",
        "what is", "what's", "how do", "how to", "how does", "why ",
        "difference", "explain", "vs ", "meaning",
    )

    @classmethod
    def _is_simple_question(cls, user_message: str) -> bool:
        """코드/기술 관련이어도 '설명/개념 질문'은 단일 모델로 처리."""
        if not user_message:
            return False
        msg = user_message.strip().lower()
        # 액션 동사가 있으면 구현 요청 — 단순 질문 아님
        for phrase in cls._CODING_ACTION_PATTERNS:
            if phrase in msg:
                return False
        # 코드 블록이 있으면 기존 코드 수정/분석 요청 — 단순 질문 아님
        if "```" in user_message:
            return False
        # 물음표로 끝나거나 설명 요청 패턴이 보이면 단순 질문
        if msg.endswith("?") or msg.endswith("?"):
            return True
        for phrase in cls._SIMPLE_QUESTION_PATTERNS:
            if phrase in msg:
                return True
        # 150자 미만의 짧은 메시지는 설명 요청일 가능성이 큼
        if len(msg) < 60 and not any(
            kw in msg for kw in ("만들", "구현", "생성", "작성", "build", "create")
        ):
            # 기술 키워드가 있으면 여전히 단순 기술 질문일 수 있음
            return True
        return False

    @classmethod
    def _is_coding_request(cls, user_message: str) -> bool:
        """사용자 메시지가 코딩/구현 요청인지 휴리스틱으로 판정.

        True  → 파이프라인(명세 → 코드) 경로
        False → 일반 채팅 응답 경로 (명세 단계 건너뜀)

        단순 설명/개념 질문은 기술 키워드가 있더라도 False 를 반환하여
        단일 모델 답변 경로로 빠지도록 합니다.
        """
        if not user_message:
            return False

        # 단순 질의는 단일 모델로 — 코딩 파이프라인 경로 아님
        if cls._is_simple_question(user_message):
            return False

        msg = user_message.lower()

        # 강한 신호 1: 액션 동사 포함
        for phrase in cls._CODING_ACTION_PATTERNS:
            if phrase in msg:
                return True

        # 강한 신호 2: 코드 블록이 포함된 경우 (기존 코드 수정 요청일 가능성)
        if "```" in user_message:
            return True

        # 약한 신호: 기술 키워드가 2개 이상 등장
        tech_hits = sum(1 for kw in cls._CODING_TECH_KEYWORDS if kw in msg)
        if tech_hits >= 2:
            return True

        return False

    CHAT_SYSTEM_PROMPT = (
        "당신은 친절하고 간결한 한국어 어시스턴트입니다. "
        "사용자의 질문에 핵심만 명료하게 답하고, 필요한 경우에만 예시를 드세요. "
        "코드가 필요한 경우에만 코드 블록을 사용하고, 불필요하게 길게 쓰지 마십시오. "
        "\"참고 자료\" 블록이 제공되면 그 안의 정보를 우선으로 사용하고, "
        "출처가 있는 경우 답변 끝에 '(출처: ...)' 형태로 표기하세요."
    )

    # ------------------------------------------------------------------
    # 외부 데이터 수집 (URL fetch + DuckDuckGo 검색)
    # ------------------------------------------------------------------
    _URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
    _REALTIME_TRIGGERS = (
        "지금", "현재", "오늘", "최근", "최신", "방금", "며칠",
        "news", "today", "latest", "current", "price", "시세",
        "환율", "날씨", "주가", "코스피", "코스닥", "비트코인",
        "뉴스", "경기", "스코어", "일정",
    )

    @classmethod
    def _needs_external_data(cls, user_message: str) -> bool:
        if not user_message:
            return False
        if cls._URL_RE.search(user_message):
            return True
        low = user_message.lower()
        return any(trig.lower() in low for trig in cls._REALTIME_TRIGGERS)

    @staticmethod
    def _strip_html(html: str, limit: int = 2000) -> str:
        # <script>, <style> 제거
        html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
        html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
        # 태그 제거
        text = re.sub(r"<[^>]+>", " ", html)
        # HTML 엔티티 (일부만 간단 변환)
        text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                    .replace("&lt;", "<").replace("&gt;", ">")
                    .replace("&quot;", '"').replace("&#39;", "'"))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]

    @classmethod
    def _fetch_url(cls, url: str, timeout: float = 8.0) -> str:
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (vibe-coding fetcher)"},
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                body = resp.text
        except Exception as exc:  # noqa: BLE001
            return f"[URL 가져오기 실패: {exc}]"
        if "html" in ctype.lower():
            return cls._strip_html(body, limit=2500)
        return body[:2500]

    # ── 카테고리별 직접 소스 (Naver 금융 등) ──────────────────────────
    @classmethod
    def _fetch_naver_marketindex(cls) -> str:
        """네이버 금융 마켓 인덱스 페이지에서 환율·지수·원자재 스냅샷 추출."""
        try:
            with httpx.Client(
                timeout=8.0,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/124.0 Safari/537.36",
                },
            ) as client:
                resp = client.get("https://finance.naver.com/marketindex/")
                resp.encoding = "euc-kr"
                html = resp.text
        except Exception as exc:  # noqa: BLE001
            return f"[Naver 금융 마켓인덱스 가져오기 실패: {exc}]"

        # 각 카드: <h3 class="h_lst"><span class="blind">카테고리</span></h3>
        # 내부에 <span class="value">숫자</span>, <span class="change">변동</span>
        items: list[str] = []
        pattern = re.compile(
            r'<h3 class="h_lst"><span class="blind">([^<]+)</span></h3>'
            r'[\s\S]*?<span class="value">([^<]+)</span>'
            r'(?:[\s\S]*?<span class="change">([^<]+)</span>)?',
            re.IGNORECASE,
        )
        for match in pattern.finditer(html):
            cat = cls._strip_html(match.group(1), limit=40)
            value = cls._strip_html(match.group(2), limit=40)
            change = cls._strip_html(match.group(3) or "", limit=40)
            line = f"- {cat}: {value}"
            if change:
                line += f" (변동 {change})"
            items.append(line)
            if len(items) >= 25:
                break

        if not items:
            return "[Naver 금융 마켓인덱스에서 데이터를 추출하지 못했습니다]"
        return (
            "[Naver 금융 마켓인덱스 스냅샷]\n"
            + "\n".join(items)
            + "\n(출처: https://finance.naver.com/marketindex/)"
        )

    @classmethod
    def _fetch_naver_search(cls, query: str, timeout: float = 8.0) -> list[dict[str, str]]:
        """Naver 통합 검색 HTML 결과에서 상위 스니펫 추출."""
        results: list[dict[str, str]] = []
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/124.0 Safari/537.36",
                    "Accept-Language": "ko,en;q=0.8",
                },
            ) as client:
                resp = client.get(
                    "https://search.naver.com/search.naver",
                    params={"query": query},
                )
                resp.raise_for_status()
                html = resp.text
        except Exception:  # noqa: BLE001
            return results

        # Naver 검색 결과 카드: <a ... class="...news_tit..." href="..." title="..."> 및
        # <div class="...desc..."> 등 클래스가 자주 바뀌므로 대강의 링크 + 텍스트만 뽑음.
        for match in re.finditer(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*title="([^"]+)"',
            html,
        ):
            url = match.group(1)
            title = cls._strip_html(match.group(2), limit=120)
            if url.startswith("https://search.naver.com"):
                continue
            if any(r["url"] == url for r in results):
                continue
            results.append({"title": title, "snippet": "", "url": url})
            if len(results) >= 5:
                break

        # 스니펫 텍스트 — 일반적인 본문 스니펫 클래스 패턴 수집
        snippets: list[str] = []
        for match in re.finditer(
            r'<(?:a|div|span)[^>]+class="[^"]*(?:api_txt_lines|dsc_txt_wrap|total_dsc|news_dsc)[^"]*"[^>]*>([\s\S]*?)</(?:a|div|span)>',
            html,
        ):
            text = cls._strip_html(match.group(1), limit=300)
            if text and len(text) > 20:
                snippets.append(text)
            if len(snippets) >= 5:
                break

        for i, r in enumerate(results):
            if i < len(snippets):
                r["snippet"] = snippets[i]
        return results

    @classmethod
    def _duckduckgo_html_search(cls, query: str, timeout: float = 8.0) -> list[dict[str, str]]:
        """DuckDuckGo HTML 엔드포인트 폴백."""
        results: list[dict[str, str]] = []
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (vibe-coding fetcher)"},
            ) as client:
                resp = client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                )
                resp.raise_for_status()
                html = resp.text
        except Exception:  # noqa: BLE001
            return results

        pattern = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>'
            r'[\s\S]*?class="result__snippet"[^>]*>([\s\S]*?)</a>',
            re.IGNORECASE,
        )
        for match in pattern.finditer(html):
            if len(results) >= 5:
                break
            url = match.group(1)
            title = cls._strip_html(match.group(2), limit=120)
            snippet = cls._strip_html(match.group(3), limit=300)
            results.append({"title": title, "snippet": snippet, "url": url})
        return results

    @classmethod
    def fetch_external_context(cls, user_message: str) -> str:
        """사용자 메시지에서 URL/실시간 키워드를 감지해 참고 자료 블록 생성."""
        if not cls._needs_external_data(user_message):
            return ""

        parts: list[str] = []
        msg_low = user_message.lower()

        # 1) 메시지 안의 URL 직접 가져오기 (최대 2개)
        urls = cls._URL_RE.findall(user_message)[:2]
        for url in urls:
            body = cls._fetch_url(url)
            parts.append(f"[페이지: {url}]\n{body}")

        if not urls:
            # 2) 카테고리별 직접 소스 — 환율/코스피/코스닥/원유/금 등은
            # 한 번의 요청으로 Naver 금융 마켓인덱스에서 스냅샷을 얻을 수 있음
            finance_kws = (
                "환율", "달러", "엔화", "유로", "위안",
                "코스피", "kospi", "코스닥", "kosdaq",
                "다우", "나스닥", "s&p",
                "금값", "유가", "wti", "비트코인", "bitcoin", "이더리움", "ethereum",
                "원/달러", "exchange rate",
            )
            if any(kw in msg_low for kw in finance_kws):
                parts.append(cls._fetch_naver_marketindex())

            # 3) 그래도 비었거나 일반 질의면 네이버 검색 → (실패 시) DDG
            if not parts:
                results = cls._fetch_naver_search(user_message)
                source = "Naver 검색"
                if not results:
                    results = cls._duckduckgo_html_search(user_message)
                    source = "DuckDuckGo"
                if results:
                    # 상위 결과 1개는 본문까지 fetch 해서 깊게 읽기
                    top = results[0]
                    top_body = cls._fetch_url(top["url"])
                    lines: list[str] = [f"[{source} 상위 결과]"]
                    for i, r in enumerate(results, 1):
                        t = r.get("title", "")
                        s = r.get("snippet", "")
                        u = r.get("url", "")
                        lines.append(f"{i}. {t}\n   {s}\n   ({u})")
                    lines.append("")
                    lines.append(f"[상위 결과 본문 발췌: {top.get('url','')}]")
                    lines.append(top_body)
                    parts.append("\n".join(lines))

        if not parts:
            return ""

        return (
            "--- 참고 자료 ---\n"
            + "\n\n".join(parts)
            + "\n--- /참고 자료 ---"
        )

    # 이전 턴에 주입했던 "참고 자료" 블록은 히스토리에서 제거 (프롬프트 팽창 방지)
    _REF_BLOCK_RE = re.compile(
        r"\n*---\s*참고 자료\s*---[\s\S]*?---\s*/참고 자료\s*---\n*",
        re.IGNORECASE,
    )
    # 이전 턴 끝에 붙였던 지시문도 제거
    _REF_TAIL_RE = re.compile(
        r"\n*위 '참고 자료'[\s\S]*?거절 문구[\s\S]*?금지\.?\s*$",
    )
    # 외부 데이터 수집 상태 라인 제거
    _FETCH_STATUS_RE = re.compile(r"^🌐 외부 데이터 수집 중\.{0,3}\s*", re.MULTILINE)

    # 히스토리 길이 제한
    _HISTORY_MAX_TURNS = 8          # 최근 8개 메시지(=약 4왕복)만
    _HISTORY_PER_MSG_CHARS = 1500   # 메시지 1개 당 최대 글자수
    _HISTORY_TOTAL_CHARS = 6000     # 히스토리 전체 최대 글자수

    @classmethod
    def _normalize_chat_messages(
        cls,
        messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        if not messages:
            return normalized

        for raw in messages[-cls._HISTORY_MAX_TURNS:]:
            if not isinstance(raw, dict):
                continue
            role = raw.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = raw.get("content")
            if content is None:
                continue
            text = str(content)
            # 이전 턴 주입물 제거
            text = cls._REF_BLOCK_RE.sub("\n", text)
            text = cls._REF_TAIL_RE.sub("", text)
            text = cls._FETCH_STATUS_RE.sub("", text)
            text = text.strip()
            if not text:
                continue
            # 메시지 1개가 너무 길면 앞/뒤만 남기고 중간 생략
            if len(text) > cls._HISTORY_PER_MSG_CHARS:
                head = text[: cls._HISTORY_PER_MSG_CHARS // 2]
                tail = text[-cls._HISTORY_PER_MSG_CHARS // 2 :]
                text = f"{head}\n...(중략)...\n{tail}"
            normalized.append({"role": role, "content": text})

        # 전체 글자수 제한 — 뒤(최신)부터 누적해서 넘으면 앞(오래된) 쪽을 버림
        total = 0
        kept_reversed: list[dict[str, str]] = []
        for msg in reversed(normalized):
            total += len(msg["content"])
            if total > cls._HISTORY_TOTAL_CHARS and kept_reversed:
                break
            kept_reversed.append(msg)
        return list(reversed(kept_reversed))

    def _build_model_messages(
        self,
        system: str,
        user: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, str]]:
        conversation = self._normalize_chat_messages(messages)
        user = (user or "").strip()
        if user:
            if not conversation or conversation[-1]["role"] != "user" or conversation[-1]["content"] != user:
                conversation.append({"role": "user", "content": user})
        return [{"role": "system", "content": system}, *conversation]

    def generate_chat_reply(
        self, user_message: str, messages: list[dict[str, Any]] | None = None
    ) -> Generator[str, None, None]:
        """일반 질문에 대한 간단한 채팅 응답 (명세/코드 단계 없이 1-pass)."""
        missing = self._missing_config()
        if missing:
            yield "설정이 비어 있습니다: " + ", ".join(missing)
            return

        # coder 모델을 간단 질의응답용으로 재사용 (실시간 스트리밍)
        _, coder_model = self._select_models(user_message)

        # 외부 데이터가 필요한 질문이면 URL/검색을 시도해서 시스템 프롬프트에 주입
        system_prompt = self.CHAT_SYSTEM_PROMPT
        external: str = ""
        if self._needs_external_data(user_message):
            yield "🌐 외부 데이터 수집 중...\n\n"
            try:
                external = self.fetch_external_context(user_message)
            except Exception as exc:  # noqa: BLE001
                external = ""
                yield f"(외부 데이터 수집 실패: {exc})\n\n"
        effective_user = user_message
        if external:
            system_prompt = (
                f"{self.CHAT_SYSTEM_PROMPT}\n\n"
                "[규칙] 사용자 메시지에 '참고 자료' 블록이 포함되어 있다면, "
                "이는 방금 서버가 실제 웹에서 가져온 최신 수치/가격/지수/뉴스입니다. "
                "절대로 '실시간 정보를 제공할 수 없습니다' / '실시간 환율 정보는 제공해 드릴 수 없습니다' "
                "같은 거절 문구를 쓰지 마십시오. 반드시 자료 안의 수치를 직접 인용해 답하고, "
                "답변 끝에 (출처: URL) 형태로 1~2개 출처를 덧붙이세요. "
                "자료에 해당 항목이 없을 때만 '자료에 없음'이라고 말하세요."
            )
            effective_user = (
                f"{user_message}\n\n"
                f"{external}\n\n"
                "위 '참고 자료' 안의 수치를 사용해서 위 질문에 한국어로 바로 답하세요. "
                "거절 문구(예: '실시간 정보 제공 불가') 금지."
            )

        try:
            # 과거 대화에 '실시간 불가' 거절 답변이 들어 있으면 외부 데이터를 무시하고
            # 같은 거절을 반복하는 경향이 있으므로, 외부 데이터가 있을 때는
            # 이전 대화 맥락을 빼고 단발성으로 답하게 함.
            effective_messages = None if external else messages
            for tok in self._stream_ollama_sync(
                model=coder_model,
                system=system_prompt,
                user=effective_user,
                messages=effective_messages,
            ):
                yield tok
        except Exception as exc:
            yield self._format_model_error("응답 생성", exc)
            return

    # ------------------------------------------------------------------
    # Complexity estimation for auto-routing
    # ------------------------------------------------------------------
    @staticmethod
    def _estimate_complexity(user_message: str) -> str:
        """Estimate request complexity: 'simple' or 'complex'."""
        indicators = [
            "데이터베이스", "DB", "인증", "로그인", "API", "백엔드",
            "서버", "배포", "Docker", "Next.js", "React", "라우팅",
            "상태 관리", "Redux", "database", "authentication",
            "WebSocket", "실시간", "대시보드", "관리자",
        ]
        message_lower = user_message.lower()
        hits = sum(1 for ind in indicators if ind.lower() in message_lower)
        word_count = len(user_message.split())
        if hits >= 2 or word_count > 80:
            return "complex"
        return "simple"

    def _select_models(self, user_message: str) -> tuple[str, str]:
        """Return (spec_model, coder_model) based on auto-routing."""
        if not self.valves.auto_route:
            return self.valves.spec_model, self.valves.coder_model

        complexity = self._estimate_complexity(user_message)
        if complexity == "simple" and self.valves.spec_model_light and self.valves.coder_model_light:
            return self.valves.spec_model_light, self.valves.coder_model_light
        return self.valves.spec_model, self.valves.coder_model

    # ------------------------------------------------------------------
    # Main pipeline: initial generation
    # ------------------------------------------------------------------
    def pipe(
        self,
        user_message: str,
        model_id: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        body: dict[str, Any] | None = None,
        username: str = "anonymous",
        chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        del model_id, body

        missing = self._missing_config()
        if missing:
            yield (
                "설정이 비어 있습니다. `pipelines/config.json` 에서 "
                f"{', '.join(missing)} 값을 입력한 뒤 다시 실행해 주세요."
            )
            return

        # 코딩 요청이 아니면 명세/코드 단계를 건너뛰고 바로 채팅 응답
        if not self._is_coding_request(user_message):
            yield from self.generate_chat_reply(user_message, messages=messages)
            return

        spec_model, coder_model = self._select_models(user_message)

        if self.valves.single_pass:
            buf = ""
            try:
                for tok in self._stream_ollama_sync(
                    model=coder_model,
                    system=self.SINGLE_PASS_SYSTEM_PROMPT,
                    user=user_message,
                    messages=messages,
                ):
                    buf += tok
                    if "\n" in tok:
                        yield buf
                        buf = ""
            except Exception as exc:
                if buf:
                    yield buf
                yield self._format_model_error("코드 생성", exc)
                return
            if buf:
                yield buf
            return

        # ── 1) Plan 모델: 자연어 → JSON 명세 ───────────────────────────
        # keep_alive=0: VRAM 절약을 위해 spec 생성 직후 plan 모델을 언로드
        # 하여 이어지는 coder 모델이 로드될 수 있도록 합니다.
        yield "📝 명세서 작성 중... (plan 모델)\n\n"
        try:
            spec_response = self._run_async(self._call_model(
                model=spec_model,
                system=self.SPEC_SYSTEM_PROMPT,
                user=user_message,
                messages=messages,
                keep_alive=0,
            ))
        except Exception as exc:
            yield self._format_model_error("요청 분석", exc)
            return

        try:
            spec_json = self._parse_json(spec_response)
            self._validate_spec(spec_json)
        except ValueError as exc:
            yield (
                "요청 분석 결과를 처리하지 못했습니다: "
                f"{exc}\n\n원본 응답:\n```\n{spec_response[:2000]}\n```"
            )
            return

        # 사용자별 명세서 저장
        try:
            saved_path = self.save_spec(
                username=username,
                chat_id=chat_id,
                spec=spec_json,
                user_message=user_message,
            )
            yield (
                f"✅ 명세서 저장 완료: `specs/{saved_path.parent.name}/{saved_path.name}`\n\n"
            )
        except OSError as exc:
            yield f"⚠️ 명세서 저장 실패(무시하고 계속 진행): {exc}\n\n"

        # 명세서 미리보기 (접힌 형태로)
        spec_preview = json.dumps(spec_json, ensure_ascii=False, indent=2)
        yield (
            "<details><summary>📋 생성된 명세서 (클릭하여 펼치기)</summary>\n\n"
            f"```json\n{spec_preview}\n```\n\n</details>\n\n"
        )

        # ── 2) Coder 모델: 명세서 기반 코드 생성 (실시간 스트리밍) ───────
        yield "💻 코드 생성 중... (coder 모델)\n\n"
        buf = ""
        try:
            for tok in self._stream_ollama_sync(
                model=coder_model,
                system=self.CODER_SYSTEM_PROMPT,
                user=(
                    "다음 기술 명세서를 기반으로 코드를 생성하세요:\n"
                    f"{json.dumps(spec_json, ensure_ascii=False, indent=2)}"
                ),
                messages=messages,
            ):
                buf += tok
                if "\n" in tok:
                    yield buf
                    buf = ""
        except Exception as exc:
            if buf:
                yield buf
            yield self._format_model_error("코드 생성", exc)
            return
        if buf:
            yield buf

    # ------------------------------------------------------------------
    # Two-stage (confirm) pipeline
    # ------------------------------------------------------------------
    def generate_spec(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
        username: str = "anonymous",
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        """사용자 요청 → JSON 명세만 생성하여 저장한 뒤 dict로 반환.

        '확인 후 진행' 모드에서 사용자에게 1차 결과를 보여주기 위한 엔드포인트용.
        코드 생성은 별도 호출(`generate_code_from_spec`)로 이어집니다.
        """
        missing = self._missing_config()
        if missing:
            raise RuntimeError(
                "설정이 비어 있습니다: " + ", ".join(missing)
            )

        spec_model, _ = self._select_models(user_message)
        # keep_alive=0: spec 생성 후 즉시 언로드하여 VRAM 확보
        spec_response = self._run_async(self._call_model(
            model=spec_model,
            system=self.SPEC_SYSTEM_PROMPT,
            user=user_message,
            messages=messages,
            keep_alive=0,
        ))

        try:
            spec_json = self._parse_json(spec_response)
            self._validate_spec(spec_json)
        except ValueError as exc:
            raise ValueError(
                f"명세서 파싱 실패: {exc}\n원본 응답:\n{spec_response[:2000]}"
            ) from exc

        self.save_spec(
            username=username,
            chat_id=chat_id,
            spec=spec_json,
            user_message=user_message,
        )
        return spec_json

    def generate_code_from_spec(
        self,
        spec: dict[str, Any],
        user_message: str = "",
        messages: list[dict[str, Any]] | None = None,
        username: str = "anonymous",
        chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        """주어진 (사용자 검토/수정된) 명세서 → 코드 스트림."""
        del username, chat_id  # 현재는 사용하지 않음 (추후 로깅에 사용 가능)
        missing = self._missing_config()
        if missing:
            yield "설정이 비어 있습니다: " + ", ".join(missing)
            return

        try:
            self._validate_spec(spec)
        except ValueError as exc:
            yield f"⚠️ 명세서 검증 실패: {exc}"
            return

        _, coder_model = self._select_models(user_message or "")
        # 실시간 스트리밍 — 토큰을 버퍼에 쌓다가 ```파일경로 포맷 변환 가능한
        # 단위(개행)로 플러시해서 내보냅니다.
        buffer = ""
        try:
            for tok in self._stream_ollama_sync(
                model=coder_model,
                system=self.CODER_SYSTEM_PROMPT,
                user=(
                    "다음 기술 명세서를 기반으로 코드를 생성하세요:\n"
                    f"{json.dumps(spec, ensure_ascii=False, indent=2)}"
                ),
                messages=messages,
            ):
                buffer += tok
                # 개행이 나올 때마다 플러시 — 스트리밍 체감 속도 확보
                if "\n" in tok:
                    yield buffer
                    buffer = ""
        except Exception as exc:
            if buffer:
                yield buffer
            yield self._format_model_error("코드 생성", exc)
            return
        if buffer:
            yield buffer

    # ------------------------------------------------------------------
    # Diff pipeline: modification requests
    # ------------------------------------------------------------------
    def pipe_modify(
        self,
        modification_request: str,
        original_spec: dict[str, Any],
        existing_files: dict[str, str],
        project_id: str = "",
    ) -> Generator[str, None, None]:
        """Handle modification requests via diff spec pipeline."""
        del project_id
        spec_model, coder_model = self._select_models(modification_request)

        diff_input = (
            f"원본 명세서:\n{json.dumps(original_spec, ensure_ascii=False, indent=2)}\n\n"
            f"사용자 수정 요청:\n{modification_request}"
        )

        try:
            diff_response = self._run_async(self._call_model(
                model=spec_model,
                system=self.DIFF_SPEC_PROMPT,
                user=diff_input,
            ))
        except Exception as exc:
            yield self._format_model_error("수정 요청 분석", exc)
            return

        try:
            diff_json = self._parse_json(diff_response)
            self._validate_diff_spec(diff_json)
        except ValueError as exc:
            yield f"수정 요청 분석 결과를 처리하지 못했습니다: {exc}"
            return

        # Build coder input: existing code + diff spec
        existing_code_text = ""
        for path, content in existing_files.items():
            existing_code_text += f"```{path}\n{content}\n```\n\n"

        coder_input = (
            f"기존 코드:\n{existing_code_text}\n"
            f"diff 명세서:\n{json.dumps(diff_json, ensure_ascii=False, indent=2)}\n\n"
            f"위 diff 명세서에 명시된 변경 사항만 반영하여 수정된 파일만 출력하세요."
        )

        try:
            code_response = self._run_async(self._call_model(
                model=coder_model,
                system=self.DIFF_CODER_PROMPT,
                user=coder_input,
            ))
        except Exception as exc:
            yield self._format_model_error("코드 수정", exc)
            return

        # Open WebUI에서 ```파일경로 형식이 깨지는 문제 수정
        yield from self._stream_text(self._format_code_for_webui(code_response))

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _missing_config(self) -> list[str]:
        required = {
            "spec_model": self.valves.spec_model,
            "coder_model": self.valves.coder_model,
            "ollama_url": self.valves.ollama_url,
        }
        return [name for name, value in required.items() if not value.strip()]

    @staticmethod
    def _validate_spec(spec: dict[str, Any]) -> None:
        required = {"project", "files", "components", "api", "constraints", "user_story"}
        missing = sorted(required - set(spec))
        if missing:
            raise ValueError(f"Missing required keys: {', '.join(missing)}")
        if not isinstance(spec["files"], list) or not spec["files"]:
            raise ValueError("files must be a non-empty list")
        for item in spec["files"]:
            if not isinstance(item, dict) or not item.get("path"):
                raise ValueError("each file item must include a path")
        # Validate project sub-keys
        project = spec.get("project", {})
        if not isinstance(project, dict):
            raise ValueError("project must be an object")
        for key in ("name", "type", "scope", "tech_stack", "description"):
            if key not in project:
                raise ValueError(f"project.{key} is required")
        allowed_scopes = {"frontend", "backend", "fullstack", "cli", "data", "general"}
        if project.get("scope") not in allowed_scopes:
            raise ValueError(
                f"project.scope must be one of: {', '.join(sorted(allowed_scopes))}"
            )

    @staticmethod
    def _validate_diff_spec(diff: dict[str, Any]) -> None:
        allowed_keys = {"modified_files", "new_constraints", "removed_components"}
        if not any(key in diff for key in allowed_keys):
            raise ValueError(
                f"diff spec must contain at least one of: {', '.join(sorted(allowed_keys))}"
            )
        if "modified_files" in diff:
            if not isinstance(diff["modified_files"], list):
                raise ValueError("modified_files must be a list")
            for item in diff["modified_files"]:
                if not isinstance(item, dict) or not item.get("path"):
                    raise ValueError("each modified_file must have a path")

    # ------------------------------------------------------------------
    # Model calls
    # ------------------------------------------------------------------
    async def _call_model(
        self,
        model: str,
        system: str,
        user: str,
        messages: list[dict[str, Any]] | None = None,
        keep_alive: Any = None,
    ) -> str:
        return await self._call_ollama(
            model=model,
            system=system,
            user=user,
            messages=messages,
            keep_alive=keep_alive,
        )

    async def _call_ollama(
        self,
        model: str,
        system: str,
        user: str,
        messages: list[dict[str, Any]] | None = None,
        keep_alive: Any = None,
    ) -> str:
        request_body: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": self._build_model_messages(
                system=system,
                user=user,
                messages=messages,
            ),
        }
        if keep_alive is not None:
            request_body["keep_alive"] = keep_alive

        async with httpx.AsyncClient(timeout=600) as client:
            response = await client.post(
                f"{self.valves.ollama_url.rstrip('/')}/api/chat",
                json=request_body,
            )
            response.raise_for_status()
            payload = response.json()

        try:
            return payload["message"]["content"]
        except KeyError as exc:
            raise RuntimeError(f"Unexpected Ollama response: {payload}") from exc

    # ------------------------------------------------------------------
    # Real streaming (Ollama stream: true) — sync generator bridge
    # ------------------------------------------------------------------
    def _stream_ollama_sync(
        self,
        model: str,
        system: str,
        user: str,
        messages: list[dict[str, Any]] | None = None,
        keep_alive: Any = None,
    ) -> Generator[str, None, None]:
        """Ollama `/api/chat` 를 stream=True 로 호출하여 토큰을 바로 yield.

        내부적으로는 asyncio 루프를 별도 스레드에서 돌리고, httpx 의 라인
        이터레이터를 큐로 넘겨 동기 제너레이터로 브릿지합니다.
        """
        import queue

        request_body: dict[str, Any] = {
            "model": model,
            "stream": True,
            "messages": self._build_model_messages(
                system=system,
                user=user,
                messages=messages,
            ),
        }
        if keep_alive is not None:
            request_body["keep_alive"] = keep_alive

        q: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=64)
        url = f"{self.valves.ollama_url.rstrip('/')}/api/chat"

        async def _producer() -> None:
            try:
                async with httpx.AsyncClient(timeout=600) as client:
                    async with client.stream("POST", url, json=request_body) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            chunk = (data.get("message") or {}).get("content") or ""
                            if chunk:
                                q.put(("chunk", chunk))
                            if data.get("done"):
                                break
            except Exception as exc:  # noqa: BLE001
                q.put(("error", exc))
            finally:
                q.put(("done", None))

        def _run_loop() -> None:
            try:
                asyncio.run(_producer())
            except Exception as exc:  # noqa: BLE001
                q.put(("error", exc))
                q.put(("done", None))

        t = threading.Thread(target=_run_loop, daemon=True)
        t.start()
        while True:
            kind, value = q.get()
            if kind == "chunk":
                yield value
            elif kind == "error":
                raise value  # type: ignore[misc]
            else:  # done
                break

    # ------------------------------------------------------------------
    # Per-user spec persistence
    # ------------------------------------------------------------------
    @staticmethod
    def _sanitize_path_component(value: str) -> str:
        """Sanitize a string so it is safe to use as a filename."""
        cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", value.strip())
        return cleaned.strip("._") or "default"

    @classmethod
    def _spec_dir(cls, username: str) -> Path:
        safe = cls._sanitize_path_component(username or "anonymous")
        path = SPECS_ROOT / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    # 명세는 날짜별 jsonl 한 파일로 묶어서 관리
    # 예: specs/admin/20260414.jsonl  (한 줄 = 한 명세 레코드)
    _SPEC_DAYFILE_RE = re.compile(r"^\d{8}\.jsonl$")
    # spec 내부 키 정렬 순서 — 보기 좋게 고정
    _SPEC_KEY_ORDER = (
        "project", "files", "components", "data_model",
        "api", "logic", "styling", "constraints", "notes",
    )
    # 레코드 최상위 키 순서
    _RECORD_KEY_ORDER = (
        "saved_at", "chat_id", "user_message", "spec",
    )

    @classmethod
    def _spec_dayfile(cls, username: str, when: datetime | None = None) -> Path:
        d = cls._spec_dir(username)
        return d / ((when or datetime.now()).strftime("%Y%m%d") + ".jsonl")

    @classmethod
    def _ordered_spec(cls, spec: dict[str, Any]) -> dict[str, Any]:
        """spec 키를 정해진 순서로 재배열 (보기 좋게)."""
        if not isinstance(spec, dict):
            return spec
        ordered: dict[str, Any] = {}
        for k in cls._SPEC_KEY_ORDER:
            if k in spec:
                ordered[k] = spec[k]
        for k, v in spec.items():
            if k not in ordered:
                ordered[k] = v
        return ordered

    @classmethod
    def _ordered_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        ordered: dict[str, Any] = {}
        for k in cls._RECORD_KEY_ORDER:
            if k in record:
                ordered[k] = record[k]
        for k, v in record.items():
            if k not in ordered:
                ordered[k] = v
        if isinstance(ordered.get("spec"), dict):
            ordered["spec"] = cls._ordered_spec(ordered["spec"])
        return ordered

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
        except OSError:
            return []
        return out

    @staticmethod
    def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
        lines = [
            json.dumps(r, ensure_ascii=False, separators=(",", ":"))
            for r in records
        ]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    @classmethod
    def _find_chat_in_dayfiles(
        cls, username: str, chat_id: str | None
    ) -> tuple[Path, int] | None:
        """모든 일자별 파일에서 chat_id 일치 레코드 위치(파일, 인덱스) 검색.
        가장 최근 파일부터 검색해서 첫 일치 반환."""
        if not chat_id:
            return None
        d = cls._spec_dir(username)
        for f in sorted(d.glob("*.jsonl"), reverse=True):
            if not cls._SPEC_DAYFILE_RE.match(f.name):
                continue
            recs = cls._read_jsonl(f)
            for i, rec in enumerate(recs):
                if rec.get("chat_id") == chat_id:
                    return f, i
        return None

    @classmethod
    def save_spec(
        cls,
        username: str,
        chat_id: str | None,
        spec: dict[str, Any],
        user_message: str = "",
    ) -> Path:
        d = cls._spec_dir(username)
        record = cls._ordered_record({
            "saved_at": _now_iso(),
            "chat_id": chat_id,
            "user_message": user_message,
            "spec": spec,
        })

        # 기존 동일 chat_id 레코드가 있으면 그 파일에서 갱신,
        # 없으면 오늘 날짜 jsonl에 append
        found = cls._find_chat_in_dayfiles(username, chat_id)
        if found is not None:
            path, idx = found
            recs = cls._read_jsonl(path)
            recs[idx] = record
        else:
            path = cls._spec_dayfile(username)
            recs = cls._read_jsonl(path)
            recs.append(record)
        cls._write_jsonl(path, recs)

        # 사람이 한눈에 보기 좋은 latest.json (pretty-printed)
        latest = d / "latest.json"
        latest.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load_spec(
        cls, username: str, chat_id: str | None = None
    ) -> dict[str, Any] | None:
        found = cls._find_chat_in_dayfiles(username, chat_id)
        if found is not None:
            path, idx = found
            recs = cls._read_jsonl(path)
            if 0 <= idx < len(recs):
                spec = recs[idx].get("spec")
                if isinstance(spec, dict):
                    return spec
        # latest.json 폴백
        latest = cls._spec_dir(username) / "latest.json"
        if latest.exists():
            try:
                rec = json.loads(latest.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
            if isinstance(rec, dict) and isinstance(rec.get("spec"), dict):
                return rec["spec"]
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        stripped = text.strip()

        # Harmony/channel 스타일 마커 제거 (예: <|channel>thought<channel>{...},
        # <|start|>, <|message|>, <|end|>, <|return|> 등)
        stripped = re.sub(r"<\|?/?[a-zA-Z_][^>]*\|?>", "", stripped).strip()

        # 코드 펜스 추출
        if stripped.startswith("```"):
            match = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
            if match:
                stripped = match.group(1).strip()

        # 1차 시도: 그대로 파싱
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            # 2차 시도: 첫 '{'부터 brace matching으로 JSON 객체 추출
            parsed = None
            start = stripped.find("{")
            while start != -1 and parsed is None:
                depth = 0
                in_str = False
                esc = False
                for i in range(start, len(stripped)):
                    ch = stripped[i]
                    if in_str:
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                    else:
                        if ch == '"':
                            in_str = True
                        elif ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                            if depth == 0:
                                candidate = stripped[start : i + 1]
                                try:
                                    parsed = json.loads(candidate)
                                except json.JSONDecodeError:
                                    parsed = None
                                break
                if parsed is None:
                    start = stripped.find("{", start + 1)
            if parsed is None:
                raise ValueError(
                    f"JSON 객체를 찾을 수 없습니다. 응답 미리보기: {stripped[:200]!r}"
                )

        if not isinstance(parsed, dict):
            raise ValueError("Top-level JSON must be an object")
        return parsed

    @staticmethod
    def _format_code_for_webui(text: str) -> str:
        """Open WebUI 호환 코드 블록 변환.

        코딩 모델이 출력하는 ```파일경로 형식은 Open WebUI에서 언어 힌트로
        인식되지 않아 코드가 깨져 보입니다. 이 메서드는 해당 블록을
        파일명 헤더 + 표준 언어 힌트 형식으로 변환합니다.

        변환 전:
            ```index.html
            <html>...</html>
            ```
        변환 후:
            **📄 `index.html`**
            ```html
            <html>...</html>
            ```
        """
        ext_to_lang: dict[str, str] = {
            "html": "html", "htm": "html",
            "css": "css",
            "js": "javascript", "mjs": "javascript", "cjs": "javascript",
            "jsx": "javascript",
            "ts": "typescript", "tsx": "typescript",
            "py": "python",
            "json": "json",
            "md": "markdown",
            "sh": "bash", "bash": "bash",
            "yml": "yaml", "yaml": "yaml",
            "sql": "sql",
            "rs": "rust",
            "go": "go",
            "java": "java",
            "php": "php",
            "rb": "ruby",
            "swift": "swift",
            "kt": "kotlin",
            "xml": "xml",
            "svg": "xml",
            "toml": "toml",
        }

        def _replace(match: re.Match) -> str:  # type: ignore[type-arg]
            filepath = match.group(1).strip()
            code = match.group(2)
            ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
            lang = ext_to_lang.get(ext, ext)
            return f"\n**📄 `{filepath}`**\n```{lang}\n{code}```"

        # 파일 경로처럼 보이는 코드블록만 변환 (점(.) 포함 & 공백 없음)
        return re.sub(
            r"```([^\n`\s]+\.[^\n`\s]+)\n(.*?)```",
            _replace,
            text,
            flags=re.DOTALL,
        )

    def _format_model_error(self, stage: str, exc: Exception) -> str:
        message = str(exc)
        lowered = message.lower()
        if (
            "all connection attempts failed" in lowered
            or "connection refused" in lowered
            or "name or service not known" in lowered
            or "nodename nor servname" in lowered
            or isinstance(exc, httpx.ConnectError)
        ):
            return (
                f"{stage} 중 Ollama 서버({self.valves.ollama_url})에 연결하지 못했습니다.\n"
                "1) Ollama가 실행 중인지 확인하세요 (`ollama serve`).\n"
                "2) `pipelines/config.json`의 `ollama_url`이 올바른지 확인하세요.\n"
                "3) 네트워크/방화벽이 해당 포트(보통 11434)를 막고 있지 않은지 확인하세요."
            )
        if "404" in message and "model" in lowered:
            return (
                f"{stage} 중 모델을 찾을 수 없습니다: {message}\n"
                "`ollama list`로 설치된 모델 이름을 확인한 뒤 `pipelines/config.json`의 모델명을 수정하세요."
            )
        if "503" in message or "Service Unavailable" in message:
            return (
                f"{stage} 중 모델이 일시적으로 응답하지 않습니다. "
                "잠시 후 다시 시도하거나 `pipelines/config.json`에서 다른 모델로 바꿔 주세요."
            )
        if "429" in message or "Too Many Requests" in message or "rate_limit" in lowered:
            return (
                f"{stage} 중 API 사용량 제한에 걸렸습니다. "
                "잠시 후 다시 시도하거나 다른 모델을 사용해 주세요."
            )
        return f"{stage} 중 모델 호출에 실패했습니다: {message}"

    @staticmethod
    def _stream_text(text: str, chunk_size: int = 600) -> Generator[str, None, None]:
        for index in range(0, len(text), chunk_size):
            yield text[index : index + chunk_size]
