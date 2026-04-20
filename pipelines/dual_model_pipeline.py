"""Open WebUI pipeline for a two-model vibe-coding workflow.

Dual Model Pipeline - Spec Model + Coder Model separation architecture.
Orchestration only; concrete logic lives in sibling modules:

- config.py         : config.json loading
- prompts.py        : system prompts + few-shot examples
- router.py         : keyword/LLM-based request classification
- external_data.py  : URL fetch + Google search
- history.py        : chat history normalization
- ollama_client.py  : Ollama /api/chat (sync + stream)
- spec_storage.py   : per-user spec persistence
- utils.py          : parse_json, validation, formatting
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import external_data, ollama_client, router, spec_storage, utils
from .config import config_value
from .prompts import (
    CHAT_SYSTEM_PROMPT,
    CHAT_WITH_EXTERNAL_RULE,
    CODER_SYSTEM_PROMPT,
    DIFF_CODER_PROMPT,
    DIFF_SPEC_PROMPT,
    ROUTER_SYSTEM_PROMPT,
    SINGLE_PASS_SYSTEM_PROMPT,
    SPEC_SYSTEM_PROMPT,
)


class Pipeline:
    """Dual Model Pipeline with spec/coder separation and diff-based updates."""

    class Valves(BaseModel):
        model_config = ConfigDict(protected_namespaces=())

        spec_model: str = Field(
            default=str(config_value("plan_model", "spec_model", default="llama3.1:8b")),
            description="Spec model name. Ollama: llama3.1:8b",
        )
        coder_model: str = Field(
            default=str(config_value("dev_model", "coder_model", default="qwen2.5-coder:7b")),
            description="Coder model name. Ollama: qwen2.5-coder:7b",
        )
        single_pass: bool = Field(
            default=bool(config_value("single_pass", default=True)),
            description="Use one model call instead of spec+coder calls to reduce API usage.",
        )
        spec_model_light: str = Field(
            default=str(config_value("spec_model_light", default="")),
            description="Lightweight spec model for simple requests. Ollama: llama3.2:3b",
        )
        coder_model_light: str = Field(
            default=str(config_value("coder_model_light", default="")),
            description="Lightweight coder model for simple requests.",
        )
        ollama_url: str = Field(
            default=str(config_value("ollama_url", default="http://localhost:11434")),
            description="Ollama API base URL.",
        )
        auto_route: bool = Field(
            default=bool(config_value("auto_route", default=False)),
            description="Auto-select model size by request complexity",
        )

    _ROUTE_CACHE_MAX = 128

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._route_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Back-compat shims for callers that reach into class internals
    # ------------------------------------------------------------------
    @staticmethod
    def _is_coding_request(user_message: str) -> bool:
        return router.is_coding_request(user_message)

    @staticmethod
    def save_spec(
        username: str,
        chat_id: str | None,
        spec: dict[str, Any],
        user_message: str = "",
    ):
        return spec_storage.save_spec(username, chat_id, spec, user_message)

    @staticmethod
    def load_spec(username: str, chat_id: str | None = None):
        return spec_storage.load_spec(username, chat_id)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def _route_request(self, user_message: str) -> dict[str, Any]:
        if not user_message or not user_message.strip():
            return {"intent": "chat", "complexity": "simple", "needs_external_data": False}

        key = user_message.strip()
        if key in self._route_cache:
            return self._route_cache[key]

        router_model = (self.valves.spec_model or "").strip()
        if not router_model:
            route = router.keyword_fallback_route(user_message)
        else:
            try:
                raw_response = ollama_client.run_async(ollama_client.call_ollama(
                    ollama_url=self.valves.ollama_url,
                    model=router_model,
                    system=ROUTER_SYSTEM_PROMPT,
                    user=user_message,
                    messages=None,
                    keep_alive=None,
                ))
                parsed = utils.parse_json(raw_response)
                route = router.normalize_route(parsed, user_message)
            except Exception:  # noqa: BLE001
                route = router.keyword_fallback_route(user_message)

        if len(self._route_cache) >= self._ROUTE_CACHE_MAX:
            try:
                self._route_cache.pop(next(iter(self._route_cache)))
            except StopIteration:
                pass
        self._route_cache[key] = route
        return route

    def _select_models_from_route(self, route: dict[str, Any]) -> tuple[str, str]:
        if not self.valves.auto_route:
            return self.valves.spec_model, self.valves.coder_model
        if (
            route.get("complexity") == "simple"
            and self.valves.spec_model_light
            and self.valves.coder_model_light
        ):
            return self.valves.spec_model_light, self.valves.coder_model_light
        return self.valves.spec_model, self.valves.coder_model

    def _select_models(self, user_message: str) -> tuple[str, str]:
        if not self.valves.auto_route:
            return self.valves.spec_model, self.valves.coder_model
        complexity = router.estimate_complexity(user_message)
        if (
            complexity == "simple"
            and self.valves.spec_model_light
            and self.valves.coder_model_light
        ):
            return self.valves.spec_model_light, self.valves.coder_model_light
        return self.valves.spec_model, self.valves.coder_model

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

    def _format_model_error(self, stage: str, exc: Exception) -> str:
        return utils.format_model_error(stage, exc, self.valves.ollama_url)

    # ------------------------------------------------------------------
    # Chat reply (no spec/code stage)
    # ------------------------------------------------------------------
    def generate_chat_reply(
        self, user_message: str, messages: list[dict[str, Any]] | None = None
    ) -> Generator[str, None, None]:
        missing = self._missing_config()
        if missing:
            yield "설정이 비어 있습니다: " + ", ".join(missing)
            return

        route = self._route_request(user_message)
        _, coder_model = self._select_models_from_route(route)

        system_prompt = CHAT_SYSTEM_PROMPT
        external_block = ""
        if route.get("needs_external_data") or external_data.needs_external_data(user_message):
            yield "🌐 외부 데이터 수집 중...\n\n"
            try:
                external_block = external_data.fetch_external_context(user_message)
            except Exception as exc:  # noqa: BLE001
                external_block = ""
                yield f"(외부 데이터 수집 실패: {exc})\n\n"

        effective_user = user_message
        if external_block:
            system_prompt = f"{CHAT_SYSTEM_PROMPT}\n\n{CHAT_WITH_EXTERNAL_RULE}"
            effective_user = (
                f"{user_message}\n\n"
                f"{external_block}\n\n"
                "위 '참고 자료' 안의 수치를 사용해서 위 질문에 한국어로 바로 답하세요. "
                "거절 문구(예: '실시간 정보 제공 불가') 금지."
            )

        try:
            # 외부 데이터가 있을 때는 과거 거절 답변을 끌어오지 않도록 히스토리 제외
            effective_messages = None if external_block else messages
            for tok in ollama_client.stream_ollama_sync(
                ollama_url=self.valves.ollama_url,
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

        route = self._route_request(user_message)

        if route["intent"] == "chat":
            yield from self.generate_chat_reply(user_message, messages=messages)
            return

        spec_model, coder_model = self._select_models_from_route(route)

        if self.valves.single_pass or route["intent"] == "simple_code":
            buf = ""
            try:
                for tok in ollama_client.stream_ollama_sync(
                    ollama_url=self.valves.ollama_url,
                    model=coder_model,
                    system=SINGLE_PASS_SYSTEM_PROMPT,
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

        # 1) Plan 모델 — 자연어 → JSON 명세 (keep_alive=0: VRAM 절약)
        yield "📝 명세서 작성 중... (plan 모델)\n\n"
        try:
            spec_response = ollama_client.run_async(ollama_client.call_ollama(
                ollama_url=self.valves.ollama_url,
                model=spec_model,
                system=SPEC_SYSTEM_PROMPT,
                user=user_message,
                messages=messages,
                keep_alive=0,
            ))
        except Exception as exc:
            yield self._format_model_error("요청 분석", exc)
            return

        try:
            spec_json = utils.parse_json(spec_response)
            utils.validate_spec(spec_json)
        except ValueError as exc:
            yield (
                "요청 분석 결과를 처리하지 못했습니다: "
                f"{exc}\n\n원본 응답:\n```\n{spec_response[:2000]}\n```"
            )
            return

        try:
            saved_path = spec_storage.save_spec(
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

        spec_preview = json.dumps(spec_json, ensure_ascii=False, indent=2)
        yield (
            "<details><summary>📋 생성된 명세서 (클릭하여 펼치기)</summary>\n\n"
            f"```json\n{spec_preview}\n```\n\n</details>\n\n"
        )

        # 2) Coder 모델 — 명세서 기반 코드 스트리밍
        yield "💻 코드 생성 중... (coder 모델)\n\n"
        buf = ""
        try:
            for tok in ollama_client.stream_ollama_sync(
                ollama_url=self.valves.ollama_url,
                model=coder_model,
                system=CODER_SYSTEM_PROMPT,
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
        missing = self._missing_config()
        if missing:
            raise RuntimeError("설정이 비어 있습니다: " + ", ".join(missing))

        spec_model, _ = self._select_models(user_message)
        spec_response = ollama_client.run_async(ollama_client.call_ollama(
            ollama_url=self.valves.ollama_url,
            model=spec_model,
            system=SPEC_SYSTEM_PROMPT,
            user=user_message,
            messages=messages,
            keep_alive=0,
        ))

        try:
            spec_json = utils.parse_json(spec_response)
            utils.validate_spec(spec_json)
        except ValueError as exc:
            raise ValueError(
                f"명세서 파싱 실패: {exc}\n원본 응답:\n{spec_response[:2000]}"
            ) from exc

        spec_storage.save_spec(
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
        del username, chat_id
        missing = self._missing_config()
        if missing:
            yield "설정이 비어 있습니다: " + ", ".join(missing)
            return

        try:
            utils.validate_spec(spec)
        except ValueError as exc:
            yield f"⚠️ 명세서 검증 실패: {exc}"
            return

        _, coder_model = self._select_models(user_message or "")
        buffer = ""
        try:
            for tok in ollama_client.stream_ollama_sync(
                ollama_url=self.valves.ollama_url,
                model=coder_model,
                system=CODER_SYSTEM_PROMPT,
                user=(
                    "다음 기술 명세서를 기반으로 코드를 생성하세요:\n"
                    f"{json.dumps(spec, ensure_ascii=False, indent=2)}"
                ),
                messages=messages,
            ):
                buffer += tok
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
        del project_id
        spec_model, coder_model = self._select_models(modification_request)

        diff_input = (
            f"원본 명세서:\n{json.dumps(original_spec, ensure_ascii=False, indent=2)}\n\n"
            f"사용자 수정 요청:\n{modification_request}"
        )

        try:
            diff_response = ollama_client.run_async(ollama_client.call_ollama(
                ollama_url=self.valves.ollama_url,
                model=spec_model,
                system=DIFF_SPEC_PROMPT,
                user=diff_input,
            ))
        except Exception as exc:
            yield self._format_model_error("수정 요청 분석", exc)
            return

        try:
            diff_json = utils.parse_json(diff_response)
            utils.validate_diff_spec(diff_json)
        except ValueError as exc:
            yield f"수정 요청 분석 결과를 처리하지 못했습니다: {exc}"
            return

        existing_code_text = ""
        for path, content in existing_files.items():
            existing_code_text += f"```{path}\n{content}\n```\n\n"

        coder_input = (
            f"기존 코드:\n{existing_code_text}\n"
            f"diff 명세서:\n{json.dumps(diff_json, ensure_ascii=False, indent=2)}\n\n"
            f"위 diff 명세서에 명시된 변경 사항만 반영하여 수정된 파일만 출력하세요."
        )

        try:
            code_response = ollama_client.run_async(ollama_client.call_ollama(
                ollama_url=self.valves.ollama_url,
                model=coder_model,
                system=DIFF_CODER_PROMPT,
                user=coder_input,
            ))
        except Exception as exc:
            yield self._format_model_error("코드 수정", exc)
            return

        yield from utils.stream_text(utils.format_code_for_webui(code_response))
