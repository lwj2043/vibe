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
from pathlib import Path
from typing import Any

import httpx
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

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")


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
            default=str(_config_value("ollama_url", default="http://host.docker.internal:11434")),
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
    "type": "string (static-web | react-spa | node-app 등)",
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
    ) -> Generator[str, None, None]:
        del model_id, messages, body

        missing = self._missing_config()
        if missing:
            yield (
                "설정이 비어 있습니다. Open WebUI 파이프라인 설정에서 "
                f"{', '.join(missing)} 값을 입력한 뒤 다시 실행해 주세요."
            )
            return

        spec_model, coder_model = self._select_models(user_message)

        if self.valves.single_pass:
            try:
                code_response = self._run_async(self._call_model(
                    model=coder_model,
                    system=self.SINGLE_PASS_SYSTEM_PROMPT,
                    user=user_message,
                ))
            except Exception as exc:
                yield self._format_model_error("코드 생성", exc)
                return

            yield from self._stream_text(self._format_code_for_webui(code_response))
            return

        try:
            spec_response = self._run_async(self._call_model(
                model=spec_model,
                system=self.SPEC_SYSTEM_PROMPT,
                user=user_message,
            ))
        except Exception as exc:
            yield self._format_model_error("요청 분석", exc)
            return

        try:
            spec_json = self._parse_json(spec_response)
            self._validate_spec(spec_json)
        except ValueError as exc:
            yield f"요청 분석 결과를 처리하지 못했습니다: {exc}"
            return

        try:
            code_response = self._run_async(self._call_model(
                model=coder_model,
                system=self.CODER_SYSTEM_PROMPT,
                user=(
                    "다음 기술 명세서를 기반으로 코드를 생성하세요:\n"
                    f"{json.dumps(spec_json, ensure_ascii=False, indent=2)}"
                ),
            ))
        except Exception as exc:
            yield self._format_model_error("코드 생성", exc)
            return

        # Open WebUI에서 ```파일경로 형식이 깨지는 문제 수정
        yield from self._stream_text(self._format_code_for_webui(code_response))

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
    async def _call_model(self, model: str, system: str, user: str) -> str:
        return await self._call_ollama(model=model, system=system, user=user)

    async def _call_ollama(self, model: str, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{self.valves.ollama_url.rstrip('/')}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            response.raise_for_status()
            payload = response.json()

        try:
            return payload["message"]["content"]
        except KeyError as exc:
            raise RuntimeError(f"Unexpected Ollama response: {payload}") from exc

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        stripped = text.strip()
        if stripped.startswith("```"):
            match = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
            if match:
                stripped = match.group(1).strip()

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc

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
