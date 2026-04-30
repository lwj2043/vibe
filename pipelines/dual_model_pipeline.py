"""Single-model pipeline orchestration for the Vibe Coding app."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Generator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import (
    external_data,
    openai_client,
    preview_builder,
    router,
    screenshot,
    spec_storage,
    utils,
)
from .config import config_value
from .prompts import (
    CHAT_SYSTEM_PROMPT,
    CHAT_WITH_EXTERNAL_RULE,
    CODER_SYSTEM_PROMPT,
    DIFF_CODER_PROMPT,
    DIFF_SPEC_PROMPT,
    FIX_CODER_SYSTEM_PROMPT,
    FIX_USER_TEMPLATE,
    REVIEW_SYSTEM_PROMPT,
    REVIEW_USER_TEMPLATE,
    ROUTER_SYSTEM_PROMPT,
    SPEC_SYSTEM_PROMPT,
    VISUAL_REVIEW_SYSTEM_PROMPT,
    VISUAL_REVIEW_USER_TEMPLATE,
)

_STATUS_BEGIN = "\x01"
_STATUS_END = "\x02"

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _status(text: str) -> str:
    return f"{_STATUS_BEGIN}STATUS:{text}{_STATUS_END}"


class Pipeline:
    """Single-model pipeline: spec, code, review, and fixes use one endpoint."""

    class Valves(BaseModel):
        model_config = ConfigDict(protected_namespaces=())

        model: str = Field(
            default=str(config_value("model", default="gemma-4-31b-it")),
            description="OpenAI-compatible model name used for every stage.",
        )
        base_url: str = Field(
            default=str(
                config_value(
                    "base_url",
                    "openai_base_url",
                    default="http://192.168.100.13:8000/v1",
                )
            ),
            description="OpenAI-compatible base URL including /v1.",
        )
        api_key: str = Field(
            default=str(config_value("api_key", default="")),
            description="Optional API key for the endpoint.",
        )
        max_review_iterations: int = Field(
            default=int(config_value("max_review_iterations", default=1) or 1),
            description=(
                "Maximum code-review repair iterations. "
                "1 means generate once and review once without a repair pass."
            ),
        )
        review_safety_cap: int = Field(
            default=int(config_value("review_safety_cap", default=1) or 1),
            description="Hard cap to prevent infinite retry loops.",
        )
        enable_visual_review: bool = Field(
            default=bool(config_value("enable_visual_review", default=True)),
            description="If true, render generated HTML and review with screenshot input.",
        )

        # Back-compat fields retained because some callers may introspect these names.
        coder_model: str = Field(
            default=str(config_value("coder_model", "model", default="gemma-4-31b-it")),
            description="Legacy field. Single-model mode uses `model` instead.",
        )
        coder_base_url: str = Field(
            default=str(
                config_value(
                    "coder_base_url",
                    "base_url",
                    default="http://192.168.100.13:8000/v1",
                )
            ),
            description="Legacy field. Single-model mode uses `base_url` instead.",
        )

    _ROUTE_CACHE_MAX = 128

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._route_cache: dict[str, dict[str, Any]] = {}

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

    def _call(
        self,
        system: str,
        user: str,
        messages: list[dict[str, Any]] | None = None,
        images: list[bytes] | None = None,
    ) -> str:
        if images:
            return openai_client.run_async(
                openai_client.call_llm_with_image(
                    base_url=self.valves.base_url,
                    model=self.valves.model,
                    api_key=self.valves.api_key or None,
                    system=system,
                    user_text=user,
                    image_png_bytes=images,
                    messages=messages,
                )
            )
        return openai_client.run_async(
            openai_client.call_llm(
                base_url=self.valves.base_url,
                model=self.valves.model,
                api_key=self.valves.api_key or None,
                system=system,
                user=user,
                messages=messages,
            )
        )

    def _stream(
        self,
        system: str,
        user: str,
        messages: list[dict[str, Any]] | None = None,
        *,
        coder: bool = False,
        temperature: float | None = None,
    ) -> Generator[str, None, None]:
        del coder
        yield from openai_client.stream_llm_sync(
            base_url=self.valves.base_url,
            model=self.valves.model,
            api_key=self.valves.api_key or None,
            system=system,
            user=user,
            messages=messages,
            temperature=temperature,
        )

    @staticmethod
    def _strip_thinking(text: str) -> str:
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    def _route_request(self, user_message: str) -> dict[str, Any]:
        if not user_message or not user_message.strip():
            return {
                "intent": "chat",
                "complexity": "simple",
                "needs_external_data": False,
                "confidence": "high",
                "source": "empty",
            }

        key = user_message.strip()
        if key in self._route_cache:
            return self._route_cache[key]

        try:
            raw_response = self._call(
                system=ROUTER_SYSTEM_PROMPT,
                user=user_message,
            )
            parsed = utils.parse_json(raw_response)
            route = router.normalize_route(parsed, user_message)
            route["source"] = "llm"
        except Exception:
            route = router.keyword_fallback_route(user_message)
            route["confidence"] = "high"
            route["source"] = "keyword_error"
            self._cache_route(key, route)
            return route

        if route.get("confidence") == "low":
            fallback = router.keyword_fallback_route(user_message)
            fallback["confidence"] = "high"
            fallback["source"] = "keyword_lowconf"
            route = fallback

        self._cache_route(key, route)
        return route

    def _cache_route(self, key: str, route: dict[str, Any]) -> None:
        if len(self._route_cache) >= self._ROUTE_CACHE_MAX:
            try:
                self._route_cache.pop(next(iter(self._route_cache)))
            except StopIteration:
                pass
        self._route_cache[key] = route

    def _missing_config(self) -> list[str]:
        required = {
            "model": self.valves.model,
            "base_url": self.valves.base_url,
        }
        return [name for name, value in required.items() if not str(value).strip()]

    def _format_model_error(self, stage: str, exc: Exception) -> str:
        return utils.format_model_error(stage, exc, self.valves.base_url)

    def generate_chat_reply(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
        images: list[bytes] | None = None,
    ) -> Generator[str, None, None]:
        missing = self._missing_config()
        if missing:
            yield "설정값이 비어 있습니다: " + ", ".join(missing)
            return

        route = self._route_request(user_message)

        system_prompt = CHAT_SYSTEM_PROMPT
        external_block = ""
        if route.get("needs_external_data") or external_data.needs_external_data(user_message):
            yield "외부 데이터 수집 중..\n\n"
            try:
                external_block = external_data.fetch_external_context(user_message)
            except Exception as exc:
                external_block = ""
                yield f"(외부 데이터 수집 실패: {exc})\n\n"

        effective_user = user_message
        if external_block:
            system_prompt = f"{CHAT_SYSTEM_PROMPT}\n\n{CHAT_WITH_EXTERNAL_RULE}"
            effective_user = (
                f"{user_message}\n\n"
                f"{external_block}\n\n"
                "위 참고 자료를 바탕으로 한국어로 바로 답변하세요."
            )

        try:
            effective_messages = None if external_block else messages
            if images:
                reply = self._call(
                    system=system_prompt,
                    user=effective_user,
                    messages=effective_messages,
                    images=images,
                )
                if reply:
                    yield reply
            else:
                for tok in self._stream(
                    system=system_prompt,
                    user=effective_user,
                    messages=effective_messages,
                ):
                    yield tok
        except Exception as exc:
            yield self._format_model_error("응답 생성", exc)

    def _generate_spec_json(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None,
        images: list[bytes] | None = None,
    ) -> dict[str, Any]:
        raw = self._call(
            system=SPEC_SYSTEM_PROMPT,
            user=user_message,
            messages=messages,
            images=images,
        )
        logger.info("[spec] raw response (%d chars)", len(raw))
        spec = utils.parse_json(raw)
        utils.validate_spec(spec)
        return spec

    @staticmethod
    def _format_spec_checklist(spec: dict[str, Any]) -> str:
        project = spec.get("project") if isinstance(spec.get("project"), dict) else {}
        lines: list[str] = [
            "[프로젝트]",
            f"- name: {project.get('name', '')}",
            f"- type: {project.get('type', '')}",
            f"- scope: {project.get('scope', '')}",
            f"- tech_stack: {', '.join(map(str, project.get('tech_stack') or []))}",
            "",
            "[반드시 출력할 파일]",
        ]

        files = spec.get("files") if isinstance(spec.get("files"), list) else []
        for item in files:
            if isinstance(item, dict):
                lines.append(f"- {item.get('path', '')}: {item.get('role', '')}")

        lines.append("")
        lines.append("[기능별 acceptance criteria]")
        features = spec.get("features") if isinstance(spec.get("features"), list) else []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            lines.append(f"- {feature.get('id', '')}: {feature.get('description', '')}")
            criteria = feature.get("acceptance_criteria")
            if isinstance(criteria, list):
                for criterion in criteria:
                    lines.append(f"  - {criterion}")

        lines.append("")
        lines.append("[제약 조건]")
        constraints = spec.get("constraints") if isinstance(spec.get("constraints"), list) else []
        for constraint in constraints:
            lines.append(f"- {constraint}")
        return "\n".join(lines).strip()

    def _generate_code(
        self,
        spec: dict[str, Any],
        messages: list[dict[str, Any]] | None,
    ) -> Generator[str, None, None]:
        del messages
        checklist = self._format_spec_checklist(spec)
        prompt = (
            "다음 기술 명세서를 기반으로 코드를 생성하세요.\n\n"
            "아래 체크리스트의 모든 항목을 코드에 반영한 뒤 출력하세요. "
            "체크리스트는 내부 검증용이며 답변에는 따로 출력하지 마세요.\n\n"
            f"{checklist}\n\n"
            "[원본 기술 명세 JSON]\n"
            f"{json.dumps(spec, ensure_ascii=False, indent=2)}"
        )
        yield from self._stream(system=CODER_SYSTEM_PROMPT, user=prompt, temperature=0.1)

    def _fix_code(
        self,
        spec: dict[str, Any],
        previous_code: str,
        issues: list[str],
        fix_instructions: str,
        messages: list[dict[str, Any]] | None,
        screenshot_png: bytes | None = None,
        runtime_errors: list[str] | None = None,
    ) -> Generator[str, None, None]:
        del screenshot_png
        runtime_log = (
            "\n".join(f"- {err}" for err in runtime_errors)
            if runtime_errors
            else "(런타임 오류 없음)"
        )
        prompt = FIX_USER_TEMPLATE.format(
            spec=json.dumps(spec, ensure_ascii=False, indent=2),
            code=previous_code,
            issues="\n".join(f"- {issue}" for issue in issues) or "(명시된 문제 없음)",
            fix_instructions=fix_instructions or "(검수 결과를 참고하여 수정)",
            runtime_log=runtime_log,
        )
        yield from self._stream(
            system=FIX_CODER_SYSTEM_PROMPT,
            user=prompt,
            messages=messages,
            temperature=0.1,
        )

    def _parse_review_result(self, raw: str) -> dict[str, Any]:
        try:
            parsed = utils.parse_json(raw)
        except ValueError:
            return {
                "ok": False,
                "blocking_issues": ["검수 결과 JSON 파싱에 실패했습니다."],
                "minor_issues": [],
                "fix_instructions": "명세를 다시 확인하고 전체 코드를 재생성하세요.",
                "_raw": raw,
            }

        def _as_str_list(value: Any) -> list[str]:
            if not value:
                return []
            if not isinstance(value, list):
                value = [value]
            return [str(item) for item in value if str(item).strip()]

        ok = parsed.get("ok") is True
        blocking = _as_str_list(parsed.get("blocking_issues"))
        minor = _as_str_list(parsed.get("minor_issues"))
        legacy = _as_str_list(parsed.get("issues"))
        if not blocking and not minor and legacy:
            blocking = legacy

        fix_instructions = str(parsed.get("fix_instructions") or "")
        if not ok and not blocking:
            blocking = ["검수자가 ok=false 를 반환했지만 blocking issue를 명시하지 않았습니다."]
        return {
            "ok": ok,
            "blocking_issues": blocking,
            "minor_issues": minor,
            "fix_instructions": fix_instructions,
        }

    def _review_code_text(self, spec: dict[str, Any], code: str) -> dict[str, Any]:
        prompt = REVIEW_USER_TEMPLATE.format(
            spec=json.dumps(spec, ensure_ascii=False, indent=2),
            code=code,
        )
        raw = self._call(system=REVIEW_SYSTEM_PROMPT, user=prompt)
        return self._parse_review_result(raw)

    def _review_code_visual(
        self,
        spec: dict[str, Any],
        code: str,
        screenshot_png: bytes,
        runtime_errors: list[str] | None = None,
    ) -> dict[str, Any]:
        runtime_log = (
            "\n".join(f"- {err}" for err in runtime_errors)
            if runtime_errors
            else "(런타임 오류 없음)"
        )
        prompt = VISUAL_REVIEW_USER_TEMPLATE.format(
            spec=json.dumps(spec, ensure_ascii=False, indent=2),
            code=code,
            runtime_log=runtime_log,
        )
        raw = openai_client.run_async(
            openai_client.call_llm_with_image(
                base_url=self.valves.base_url,
                model=self.valves.model,
                api_key=self.valves.api_key or None,
                system=VISUAL_REVIEW_SYSTEM_PROMPT,
                user_text=prompt,
                image_png_bytes=screenshot_png,
            )
        )
        return self._parse_review_result(raw)

    def _try_screenshot(self, code: str) -> tuple[bytes, list[str]] | None:
        if not self.valves.enable_visual_review:
            return None
        files = preview_builder.extract_code_blocks(code)
        if not files:
            return None
        merged = preview_builder.build_combined_html(files)
        if not merged:
            return None
        return screenshot.render_html_with_diagnostics(merged)

    def _review_code(
        self,
        spec: dict[str, Any],
        code: str,
    ) -> tuple[dict[str, Any], str, bytes | None, list[str] | None]:
        shot = self._try_screenshot(code)
        if shot is not None:
            png, runtime_errors = shot
            try:
                review = self._review_code_visual(spec, code, png, runtime_errors)
                if runtime_errors and review.get("ok"):
                    review["ok"] = False
                    review["blocking_issues"] = list(review.get("blocking_issues", [])) + [
                        f"런타임 오류: {err}" for err in runtime_errors
                    ]
                    review["fix_instructions"] = (
                        (review.get("fix_instructions") or "")
                        + "\n브라우저 렌더링 중 발생한 오류를 해결하세요."
                    ).strip()
                return review, "visual", png, runtime_errors
            except Exception as exc:
                logger.warning("visual review failed, falling back to text review: %s", exc)
        return self._review_code_text(spec, code), "text", None, None

    def _run_spec_code_review_loop(
        self,
        spec: dict[str, Any],
        messages: list[dict[str, Any]] | None,
        prelude: str = "",
    ) -> Generator[str, None, None]:
        user_cap = int(self.valves.max_review_iterations or 0)
        safety = max(1, int(self.valves.review_safety_cap or 50))
        hard_cap = min(user_cap, safety) if user_cap > 0 else safety
        cap_label = str(user_cap) if user_cap > 0 else "∞"

        code = ""
        last_review: dict[str, Any] | None = None
        last_screenshot: bytes | None = None
        last_runtime_errors: list[str] | None = None
        final_note = ""

        for attempt in range(1, hard_cap + 1):
            if attempt == 1:
                base_label = f"코드 생성 중 ({attempt}/{cap_label}) - Gemma"
                gen = self._generate_code(spec, messages=messages)
                stage_label = "코드 생성"
            else:
                base_label = f"코드 수정 중 ({attempt}/{cap_label}) - Gemma"
                gen = self._fix_code(
                    spec=spec,
                    previous_code=code,
                    issues=(last_review or {}).get("blocking_issues", []),
                    fix_instructions=(last_review or {}).get("fix_instructions", ""),
                    messages=messages,
                    screenshot_png=last_screenshot,
                    runtime_errors=last_runtime_errors,
                )
                stage_label = "코드 수정"

            yield _status(base_label)
            buf: list[str] = []
            try:
                for i, tok in enumerate(gen):
                    buf.append(tok)
                    if (i + 1) % 40 == 0:
                        chars = sum(len(part) for part in buf)
                        yield _status(f"{base_label} - {chars:,}자 누적")
            except Exception as exc:
                yield self._format_model_error(stage_label, exc)
                return

            code = self._strip_thinking("".join(buf))
            yield _status(f"검수 중 ({attempt}/{cap_label}) - Gemma")
            try:
                review, _mode, png, runtime_errors = self._review_code(spec, code)
            except Exception:
                final_note = "자동 검수 단계가 실패해 마지막 결과를 그대로 출력합니다.\n\n"
                break

            if review["ok"]:
                yield _status("검수 통과 - 출력 중")
                break

            last_review = review
            last_screenshot = png
            last_runtime_errors = runtime_errors
        else:
            final_note = "\n\n"

        if prelude:
            yield prelude
        if final_note:
            yield final_note
        yield from utils.stream_text(code)

    def pipe(
        self,
        user_message: str,
        model_id: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        body: dict[str, Any] | None = None,
        username: str = "anonymous",
        chat_id: str | None = None,
        images: list[bytes] | None = None,
    ) -> Generator[str, None, None]:
        del model_id, body

        missing = self._missing_config()
        if missing:
            yield (
                "설정값이 비어 있습니다. `pipelines/config.json` 에서 "
                f"{', '.join(missing)} 값을 입력한 뒤 다시 실행해 주세요."
            )
            return

        route = self._route_request(user_message)
        if route["intent"] == "chat":
            yield from self.generate_chat_reply(
                user_message,
                messages=messages,
                images=images,
            )
            return

        yield _status("명세 생성 중")
        try:
            spec = self._generate_spec_json(
                user_message,
                messages=messages,
                images=images,
            )
        except ValueError as exc:
            yield f"요청 분석 결과를 처리하지 못했습니다. {exc}"
            return
        except Exception as exc:
            yield self._format_model_error("요청 분석", exc)
            return

        saved_note = ""
        try:
            saved_path = spec_storage.save_spec(
                username=username,
                chat_id=chat_id,
                spec=spec,
                user_message=user_message,
            )
            saved_note = f"명세 저장 완료: `specs/{saved_path.parent.name}/{saved_path.name}`\n\n"
        except OSError as exc:
            saved_note = f"명세 저장 실패(무시하고 계속 진행): {exc}\n\n"

        spec_preview = json.dumps(spec, ensure_ascii=False, indent=2)
        spec_prelude = (
            saved_note
            + "<details><summary>생성된 명세 보기</summary>\n\n"
            + f"```json\n{spec_preview}\n```\n\n</details>\n\n"
        )
        yield from self._run_spec_code_review_loop(
            spec,
            messages=messages,
            prelude=spec_prelude,
        )

    def generate_spec(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
        username: str = "anonymous",
        chat_id: str | None = None,
        images: list[bytes] | None = None,
    ) -> dict[str, Any]:
        missing = self._missing_config()
        if missing:
            raise RuntimeError("설정값이 비어 있습니다: " + ", ".join(missing))

        spec = self._generate_spec_json(
            user_message,
            messages=messages,
            images=images,
        )
        spec_storage.save_spec(
            username=username,
            chat_id=chat_id,
            spec=spec,
            user_message=user_message,
        )
        return spec

    def generate_code_from_spec(
        self,
        spec: dict[str, Any],
        user_message: str = "",
        messages: list[dict[str, Any]] | None = None,
        username: str = "anonymous",
        chat_id: str | None = None,
    ) -> Generator[str, None, None]:
        del username, chat_id, user_message
        missing = self._missing_config()
        if missing:
            yield "설정값이 비어 있습니다: " + ", ".join(missing)
            return

        try:
            utils.validate_spec(spec)
        except ValueError as exc:
            yield f"명세 검증 실패: {exc}"
            return

        yield from self._run_spec_code_review_loop(spec, messages=messages)

    def pipe_modify(
        self,
        modification_request: str,
        original_spec: dict[str, Any],
        existing_files: dict[str, str],
        project_id: str = "",
    ) -> Generator[str, None, None]:
        del project_id

        diff_input = (
            f"원본 명세:\n{json.dumps(original_spec, ensure_ascii=False, indent=2)}\n\n"
            f"수정 요청:\n{modification_request}"
        )

        try:
            diff_response = self._call(system=DIFF_SPEC_PROMPT, user=diff_input)
        except Exception as exc:
            yield self._format_model_error("수정 요청 분석", exc)
            return

        try:
            diff_json = utils.parse_json(diff_response)
            utils.validate_diff_spec(diff_json)
        except ValueError as exc:
            yield f"수정 요청 분석 결과를 처리하지 못했습니다. {exc}"
            return

        existing_code_text = ""
        for path, content in existing_files.items():
            existing_code_text += f"```{path}\n{content}\n```\n\n"

        coder_input = (
            f"기존 코드:\n{existing_code_text}\n"
            f"diff 명세:\n{json.dumps(diff_json, ensure_ascii=False, indent=2)}\n\n"
            "diff 명세에 명시된 변경 사항만 반영해 수정된 파일만 출력하세요."
        )

        try:
            code_response = self._call(system=DIFF_CODER_PROMPT, user=coder_input)
        except Exception as exc:
            yield self._format_model_error("코드 수정", exc)
            return

        yield from utils.stream_text(utils.format_code_for_webui(code_response))
