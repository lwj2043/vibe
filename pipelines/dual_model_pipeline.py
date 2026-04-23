"""Single-VLM pipeline: spec → code → review → (fix → re-review) loop.

One VLM model (OpenAI-compatible endpoint) performs every stage:

1. 명세서 작성      - 자연어 요청 → 구조화된 JSON 명세
2. 코드 작성        - 명세 → 파일별 코드
3. 검토            - 생성 코드가 명세/구문을 충족하는지 모델이 직접 판정
4. 정상이면 출력    - 검토 통과 시 코드를 사용자에게 전달
5. 문제 있으면 수정 - 검토가 제기한 문제를 반영한 코드 재생성 → 다시 3번으로

Orchestration only; concrete logic lives in sibling modules:

- config.py         : config.json loading
- prompts.py        : system prompts + few-shot examples
- router.py         : keyword/LLM-based request classification
- external_data.py  : URL fetch + Google search
- history.py        : chat history normalization
- openai_client.py  : OpenAI-compatible /v1/chat/completions (sync + stream)
- spec_storage.py   : per-user spec persistence
- utils.py          : parse_json, validation, formatting
"""

from __future__ import annotations

import json
import logging
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
# ──────────────────────────────────────────────────────────────────────────
# 진행-상태 프레임 프로토콜 (서버 → 프런트)
#
# 중간 진행 상황은 `\x01STATUS:<text>\x02` 형태의 인라인 프레임으로 전송한다.
# 프런트는 이 프레임만 뽑아 로딩 버블 라벨을 갱신하고, 프레임이 아닌 바이트만
# 실제 메시지 본문으로 누적한다. 즉, 최종 검토가 끝나기 전까지는 스트리밍
# 버블이 뜨지 않고 "잘 생각하기" 로딩이 계속 유지된다.
# ──────────────────────────────────────────────────────────────────────────
_STATUS_BEGIN = "\x01"
_STATUS_END = "\x02"

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _status(text: str) -> str:
    return f"{_STATUS_BEGIN}STATUS:{text}{_STATUS_END}"


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


class Pipeline:
    """Single-VLM pipeline with spec → code → review → fix loop."""

    class Valves(BaseModel):
        model_config = ConfigDict(protected_namespaces=())

        model: str = Field(
            default=str(config_value("model", default="gemma-4-31b-it")),
            description="VLM 모델명 (OpenAI 호환 API에 전달).",
        )
        base_url: str = Field(
            default=str(
                config_value(
                    "base_url", "openai_base_url", default="http://192.168.100.13:8000/v1"
                )
            ),
            description="OpenAI 호환 API base URL. /v1 까지 포함합니다.",
        )
        api_key: str = Field(
            default=str(config_value("api_key", default="")),
            description="API 키 (필요 없는 엔드포인트면 빈 문자열).",
        )
        max_review_iterations: int = Field(
            default=int(config_value("max_review_iterations", default=0) or 0),
            description=(
                "검토 → 수정 루프의 최대 반복 횟수. "
                "0 이면 명세를 충족할 때까지 무제한 (review_safety_cap 으로만 차단)."
            ),
        )
        review_safety_cap: int = Field(
            default=int(config_value("review_safety_cap", default=50) or 50),
            description="무한 루프 안전망. 모델 발산 시 이 횟수에서 강제 종료.",
        )
        enable_visual_review: bool = Field(
            default=bool(config_value("enable_visual_review", default=True)),
            description=(
                "HTML 기반 프로젝트일 때 Playwright 로 스크린샷을 찍어 "
                "VLM 에 이미지로 첨부 검토. Playwright 미설치 시 자동으로 텍스트 검토."
            ),
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
    # LLM helpers (bound to Valves)
    # ------------------------------------------------------------------
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
    ) -> Generator[str, None, None]:
        yield from openai_client.stream_llm_sync(
            base_url=self.valves.base_url,
            model=self.valves.model,
            api_key=self.valves.api_key or None,
            system=system,
            user=user,
            messages=messages,
        )

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
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

        # 1) 1차: LLM 분류
        try:
            raw_response = self._call(
                system=ROUTER_SYSTEM_PROMPT,
                user=user_message,
            )
            parsed = utils.parse_json(raw_response)
            route = router.normalize_route(parsed, user_message)
            route["source"] = "llm"
        except Exception:  # noqa: BLE001
            # 파싱/호출 실패 → 키워드 폴백
            route = router.keyword_fallback_route(user_message)
            route["confidence"] = "high"
            route["source"] = "keyword_error"
            self._cache_route(key, route)
            return route

        # 2) LLM이 "자신 없음" 으로 답하면 키워드 결과로 전체 덮어쓰기
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

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _missing_config(self) -> list[str]:
        required = {
            "model": self.valves.model,
            "base_url": self.valves.base_url,
        }
        return [name for name, value in required.items() if not str(value).strip()]

    def _format_model_error(self, stage: str, exc: Exception) -> str:
        return utils.format_model_error(stage, exc, self.valves.base_url)

    # ------------------------------------------------------------------
    # Chat reply (no spec/code stage)
    # ------------------------------------------------------------------
    def generate_chat_reply(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
        images: list[bytes] | None = None,
    ) -> Generator[str, None, None]:
        missing = self._missing_config()
        if missing:
            yield "설정이 비어 있습니다: " + ", ".join(missing)
            return

        route = self._route_request(user_message)

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
            effective_messages = None if external_block else messages
            if images:
                # 이미지 첨부 시 멀티모달 (비스트리밍) → 전체 응답을 한 번에 yield
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
            return

    # ------------------------------------------------------------------
    # Core: spec → code → review → (fix → re-review) loop
    # ------------------------------------------------------------------
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
        logger.info("[spec] raw response (%d chars):\n%s", len(raw), raw)
        try:
            spec = utils.parse_json(raw)
        except ValueError as exc:
            logger.error("[spec] parse_json failed: %s | raw preview=%r", exc, raw[:500])
            raise
        logger.info("[spec] parsed top-level keys: %s", sorted(spec.keys()))
        try:
            utils.validate_spec(spec)
        except ValueError as exc:
            logger.error(
                "[spec] validate_spec failed: %s | actual keys=%s | spec preview=%s",
                exc,
                sorted(spec.keys()),
                json.dumps(spec, ensure_ascii=False)[:500],
            )
            raise ValueError(
                f"{exc} | 실제 키={sorted(spec.keys())} | 미리보기={json.dumps(spec, ensure_ascii=False)[:300]}"
            ) from exc
        return spec

    def _generate_code(
        self,
        spec: dict[str, Any],
        messages: list[dict[str, Any]] | None,
    ) -> Generator[str, None, None]:
        prompt = (
            "다음 기술 명세서를 기반으로 코드를 생성하세요:\n"
            f"{json.dumps(spec, ensure_ascii=False, indent=2)}"
        )
        yield from self._stream(
            system=CODER_SYSTEM_PROMPT,
            user=prompt,
            messages=messages,
        )

    def _fix_code(
        self,
        spec: dict[str, Any],
        previous_code: str,
        issues: list[str],
        fix_instructions: str,
        messages: list[dict[str, Any]] | None,
    ) -> Generator[str, None, None]:
        prompt = FIX_USER_TEMPLATE.format(
            spec=json.dumps(spec, ensure_ascii=False, indent=2),
            code=previous_code,
            issues="\n".join(f"- {issue}" for issue in issues) or "(명시된 문제 없음)",
            fix_instructions=fix_instructions or "(지시 없음 — 검토 결과를 참고하여 수정)",
        )
        yield from self._stream(
            system=FIX_CODER_SYSTEM_PROMPT,
            user=prompt,
            messages=messages,
        )

    def _parse_review_result(self, raw: str) -> dict[str, Any]:
        # 기본은 '불합격'. 모델이 명시적으로 ok=true 를 주지 않으면 통과시키지 않음.
        try:
            parsed = utils.parse_json(raw)
        except ValueError:
            return {
                "ok": False,
                "issues": ["검토 결과 JSON 파싱 실패 — 판정 불가, 재생성 필요"],
                "fix_instructions": "명세서의 모든 항목을 다시 점검하여 완전한 코드를 재생성하세요.",
                "_raw": raw,
            }
        ok = parsed.get("ok")
        ok = (ok is True)  # 문자열 "true" 나 누락은 False 로 취급
        issues = parsed.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)]
        fix_instructions = str(parsed.get("fix_instructions") or "")
        if not ok and not issues:
            issues = ["검토자가 문제를 명시하지 않았지만 ok=true 가 아니므로 불합격 처리"]
        return {
            "ok": ok,
            "issues": [str(x) for x in issues],
            "fix_instructions": fix_instructions,
        }

    def _review_code_text(self, spec: dict[str, Any], code: str) -> dict[str, Any]:
        """텍스트 전용 검토."""
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
        """스크린샷 + 런타임 로그를 첨부한 멀티모달 검토."""
        runtime_log = (
            "\n".join(f"- {err}" for err in runtime_errors)
            if runtime_errors
            else "(런타임 에러 없음)"
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
        """코드 → 단일 HTML 병합 → 헤드리스 렌더 → (PNG, 런타임 에러)."""
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
    ) -> tuple[dict[str, Any], str]:
        """검토 실행. 가능하면 스크린샷 + 런타임 로그 첨부 멀티모달, 아니면 텍스트."""
        shot = self._try_screenshot(code)
        if shot is not None:
            png, runtime_errors = shot
            try:
                review = self._review_code_visual(spec, code, png, runtime_errors)
                # 런타임 에러가 있는데 모델이 ok=true 로 판정하면 강제로 false 처리
                if runtime_errors and review.get("ok"):
                    review["ok"] = False
                    review["issues"] = list(review.get("issues", [])) + [
                        f"런타임: {err}" for err in runtime_errors
                    ]
                    review["fix_instructions"] = (
                        (review.get("fix_instructions") or "")
                        + "\n헤드리스 렌더 중 발생한 JS 에러/요청 실패를 제거하세요."
                    ).strip()
                return review, "visual"
            except Exception as exc:  # noqa: BLE001
                import sys
                print(
                    f"[visual review 실패, 텍스트로 폴백] {exc}", file=sys.stderr
                )
        return self._review_code_text(spec, code), "text"

    def _run_spec_code_review_loop(
        self,
        spec: dict[str, Any],
        messages: list[dict[str, Any]] | None,
        prelude: str = "",
    ) -> Generator[str, None, None]:
        """코드 생성 → 검토 → (문제 있으면) 수정 → 재검토 루프.

        - ``max_review_iterations`` 가 0 이면 명세/디자인을 충족할 때까지 반복.
        - ``review_safety_cap`` 으로 하드 상한을 두어 발산 시 강제 종료.
        - 사용자에게는 진행 상황만 짧게 흘려보내고, 최종 통과한 코드만 본문으로 출력한다.
        """
        user_cap = int(self.valves.max_review_iterations or 0)
        safety = max(1, int(self.valves.review_safety_cap or 50))
        hard_cap = min(user_cap, safety) if user_cap > 0 else safety
        cap_label = str(user_cap) if user_cap > 0 else "∞"

        code = ""
        last_review: dict[str, Any] | None = None
        attempt = 0
        final_note = ""
        while attempt < hard_cap:
            attempt += 1
            if attempt == 1:
                base_label = f"코드 생성 중 (1/{cap_label})"
                gen = self._generate_code(spec, messages=messages)
                stage_label = "코드 생성"
            else:
                base_label = f"코드 수정 중 ({attempt}/{cap_label})"
                gen = self._fix_code(
                    spec=spec,
                    previous_code=code,
                    issues=(last_review or {}).get("issues", []),
                    fix_instructions=(last_review or {}).get("fix_instructions", ""),
                    messages=messages,
                )
                stage_label = "코드 수정"

            yield _status(base_label)
            buf: list[str] = []
            try:
                for i, tok in enumerate(gen):
                    buf.append(tok)
                    # 긴 생성 동안에도 "살아 있음"을 알리기 위해 주기적으로 상태 갱신
                    if (i + 1) % 40 == 0:
                        chars = sum(len(p) for p in buf)
                        yield _status(f"{base_label} — {chars:,}자 누적")
            except Exception as exc:
                yield self._format_model_error(stage_label, exc)
                return
            code = "".join(buf)

            yield _status(f"코드 검토 중 ({attempt}/{cap_label})")
            try:
                review, _mode = self._review_code(spec, code)
            except Exception:
                # 검토 실패 → 마지막 코드를 그대로 출력
                final_note = "⚠️ 자동 검토 단계가 실패해 마지막 결과를 그대로 출력합니다.\n\n"
                break

            if review["ok"]:
                yield _status("✅ 검토 통과 — 결과 출력 중")
                break

            last_review = review
        else:
            # while-else: hard_cap 도달 (break 없이 종료)
            final_note = "⚠️ 최대 반복 횟수에 도달해 마지막 결과를 그대로 출력합니다.\n\n"

        # ── 여기서부터가 "프런트가 스트리밍 버블로 전환하는" 첫 평문 바이트 ──
        if prelude:
            yield prelude
        if final_note:
            yield final_note
        yield from utils.stream_text(code)

    # ------------------------------------------------------------------
    # Public: automatic mode (message → spec → code → review → output)
    # ------------------------------------------------------------------
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
                "설정이 비어 있습니다. `pipelines/config.json` 에서 "
                f"{', '.join(missing)} 값을 입력한 뒤 다시 실행해 주세요."
            )
            return

        route = self._route_request(user_message)

        if route["intent"] == "chat":
            yield from self.generate_chat_reply(
                user_message, messages=messages, images=images
            )
            return

        # 1) 명세서 작성 — 로딩 상태만 전송
        yield _status("명세서 작성 중")
        try:
            spec = self._generate_spec_json(
                user_message, messages=messages, images=images
            )
        except ValueError as exc:
            yield f"요청 분석 결과를 처리하지 못했습니다: {exc}"
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
            saved_note = (
                f"✅ 명세서 저장 완료: `specs/{saved_path.parent.name}/{saved_path.name}`\n\n"
            )
        except OSError as exc:
            saved_note = f"⚠️ 명세서 저장 실패(무시하고 계속 진행): {exc}\n\n"

        spec_preview = json.dumps(spec, ensure_ascii=False, indent=2)
        spec_prelude = (
            saved_note
            + "<details><summary>📋 생성된 명세서 (클릭하여 펼치기)</summary>\n\n"
            f"```json\n{spec_preview}\n```\n\n</details>\n\n"
        )

        # 2) 코드 생성 → 3) 검토 → 4/5) 출력 or 수정 루프
        #    루프는 중간 과정 동안 STATUS 프레임만 흘리고, 최종 통과 후에
        #    평문(= 프런트 스트리밍 전환 트리거)을 내보낸다.
        yield from self._run_spec_code_review_loop(
            spec, messages=messages, prelude=spec_prelude
        )

    # ------------------------------------------------------------------
    # Public: two-stage (confirm) pipeline
    # ------------------------------------------------------------------
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
            raise RuntimeError("설정이 비어 있습니다: " + ", ".join(missing))

        try:
            spec = self._generate_spec_json(
                user_message, messages=messages, images=images
            )
        except ValueError as exc:
            raise ValueError(f"명세서 파싱 실패: {exc}") from exc

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
            yield "설정이 비어 있습니다: " + ", ".join(missing)
            return

        try:
            utils.validate_spec(spec)
        except ValueError as exc:
            yield f"⚠️ 명세서 검증 실패: {exc}"
            return

        # 확인 후 진행 모드에서도 동일한 코드→검토→수정 루프 적용
        yield from self._run_spec_code_review_loop(spec, messages=messages)

    # ------------------------------------------------------------------
    # Diff pipeline: modification requests (single model)
    # ------------------------------------------------------------------
    def pipe_modify(
        self,
        modification_request: str,
        original_spec: dict[str, Any],
        existing_files: dict[str, str],
        project_id: str = "",
    ) -> Generator[str, None, None]:
        del project_id

        diff_input = (
            f"원본 명세서:\n{json.dumps(original_spec, ensure_ascii=False, indent=2)}\n\n"
            f"사용자 수정 요청:\n{modification_request}"
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
            code_response = self._call(system=DIFF_CODER_PROMPT, user=coder_input)
        except Exception as exc:
            yield self._format_model_error("코드 수정", exc)
            return

        yield from utils.stream_text(utils.format_code_for_webui(code_response))
