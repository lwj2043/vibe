"""OpenAI 호환 ``/v1`` 엔드포인트.

Open WebUI 가 vibe-orchestrator 를 또 하나의 OpenAI 호환 백엔드로 인식하도록
``/v1/models`` 와 ``/v1/chat/completions`` 를 노출한다. 모델 ID 에 따라
dual_model_pipeline 의 다른 메서드를 호출:

==================  ==========================================
``vibe-auto``        ``Pipeline.pipe()`` (router.py 분류기 자동)
``vibe-chat``        ``Pipeline.generate_chat_reply()``
``vibe-spec``        ``Pipeline.generate_spec()`` → JSON 텍스트
``vibe-code``        ``Pipeline.generate_code_from_spec()``
==================  ==========================================

인증은 모든 ``/v1/*`` 에서 ``Authorization: Bearer <PORT_PROJECT_INTERNAL_TOKEN>``
검사. Open WebUI 의 OpenAI 백엔드 설정에서 그대로 키로 등록 가능.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from pipelines.sse_adapter import collect_to_chat_completion, stream_to_openai_sse

from ._pipeline import pipeline

logger = logging.getLogger(__name__)

def _clean_token(value: str | None) -> str:
    return (value or "").strip().lstrip("\ufeff")


INTERNAL_TOKEN = _clean_token(
    os.getenv("PORT_PROJECT_INTERNAL_TOKEN", "") or os.getenv("VIBE_INTERNAL_TOKEN", "")
)

# 모델 ID → dual_model_pipeline 메서드 분기 키
MODEL_MAP: dict[str, str] = {
    "vibe-auto": "auto",
    "vibe-chat": "chat",
    "vibe-spec": "spec",
    "vibe-code": "code",
}

MODEL_LABELS: dict[str, str] = {
    "vibe-auto": "바이브잇",
    "vibe-chat": "수동: 일반 대화",
    "vibe-spec": "수동: 명세 작성",
    "vibe-code": "수동: 코드 생성",
}

router = APIRouter()


# ---------------------------------------------------------------------------
# 인증 / 사용자 매핑
# ---------------------------------------------------------------------------
def _check_bearer(authorization: str | None) -> None:
    """``Authorization: Bearer <token>`` 검사. dev (token 미설정) 시엔 통과."""
    if not INTERNAL_TOKEN:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="bad Authorization scheme")
    provided = _clean_token(parts[1])
    if not secrets.compare_digest(provided, INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


_SAFE_USERNAME_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def _username_from_request(body_user: str | None, header_email: str | None) -> str:
    """Open WebUI 의 ``user`` 필드(이메일) → 안전한 username.

    이메일이 들어오면 ``@`` 앞부분만 취하고 안전 문자만 남긴다. 비어 있으면
    ``anonymous``.
    """
    candidate = (body_user or header_email or "").strip()
    if not candidate:
        return "anonymous"
    name = candidate.split("@", 1)[0]
    safe = _SAFE_USERNAME_RE.sub("_", name).strip("._-")
    return safe or "anonymous"


# ---------------------------------------------------------------------------
# 메시지 파싱 (OpenAI vision content 포맷 지원)
# ---------------------------------------------------------------------------
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024


def _extract_text_and_images(content: Any) -> tuple[str, list[bytes]]:
    """OpenAI 의 ``content`` 가 list 면 text/image_url 분리.

    string 이면 그대로 (텍스트, 이미지 없음).
    """
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content or ""), []

    texts: list[str] = []
    images: list[bytes] = []
    total_bytes = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            t = part.get("text") or ""
            if isinstance(t, str):
                texts.append(t)
            continue
        if ptype != "image_url":
            continue
        if len(images) >= MAX_IMAGES:
            raise HTTPException(
                status_code=413, detail=f"이미지는 최대 {MAX_IMAGES}개까지 허용됩니다"
            )
        image_url = part.get("image_url")
        url = ""
        if isinstance(image_url, dict):
            url = image_url.get("url") or ""
        elif isinstance(image_url, str):
            url = image_url
        if not url.startswith("data:"):
            # 외부 URL 이미지는 SSRF/대역폭 우려로 1차 미지원.
            continue
        _, _, b64 = url.partition(",")
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(raw) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="이미지 한 장이 너무 큽니다")
        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="이미지 전체 크기가 너무 큽니다")
        images.append(raw)

    return "\n".join(t for t in texts if t).strip(), images


def _flatten_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """이전 턴들을 dual_model_pipeline 이 기대하는 ``[{role, content}, ...]`` 로.

    image_url 은 history 에서 텍스트만 보존(이미지는 마지막 user 턴에서만 사용).
    """
    history: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant", "system"):
            continue
        text, _ = _extract_text_and_images(m.get("content", ""))
        history.append({"role": role, "content": text})
    return history


# ---------------------------------------------------------------------------
# 요청 / 라우트
# ---------------------------------------------------------------------------
class ChatCompletionsRequest(BaseModel):
    model: str = Field(..., description="vibe-auto / vibe-chat / vibe-spec / vibe-code")
    messages: list[dict[str, Any]] = Field(..., min_length=1)
    stream: bool = False
    user: str | None = None
    temperature: float | None = None
    metadata: dict[str, Any] | None = None
    # OpenAI 표준에 있는 다른 필드(top_p, n, presence_penalty 등) 는 무시.

    model_config = {"extra": "ignore"}


@router.get("/v1/models")
def list_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_bearer(authorization)
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": mid,
                "name": MODEL_LABELS.get(mid, mid),
                "object": "model",
                "created": now,
                "owned_by": "vibe",
            }
            for mid in MODEL_MAP
        ],
    }


@router.post("/v1/chat/completions")
def chat_completions(
    payload: ChatCompletionsRequest,
    authorization: str | None = Header(default=None),
    x_openwebui_user_email: str | None = Header(
        default=None, alias="X-OpenWebUI-User-Email"
    ),
):
    _check_bearer(authorization)

    mode = MODEL_MAP.get(payload.model)
    if mode is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown model '{payload.model}'. expected one of {sorted(MODEL_MAP)}",
        )

    username = _username_from_request(payload.user, x_openwebui_user_email)
    chat_id: str | None = None
    if payload.metadata and isinstance(payload.metadata.get("chat_id"), str):
        chat_id = payload.metadata["chat_id"]

    # 마지막 user 메시지 찾기
    last_user_idx: int | None = None
    for i in range(len(payload.messages) - 1, -1, -1):
        if payload.messages[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        raise HTTPException(status_code=422, detail="no user message in payload.messages")

    last_user = payload.messages[last_user_idx]
    user_text, images = _extract_text_and_images(last_user.get("content", ""))
    if not user_text and not images:
        raise HTTPException(status_code=422, detail="empty user message")

    history = _flatten_history(payload.messages[:last_user_idx])

    def gen() -> Iterator[str]:
        if mode == "chat":
            yield from pipeline.generate_chat_reply(
                user_text,
                messages=history or None,
                images=images or None,
            )
            return

        if mode == "spec":
            try:
                spec = pipeline.generate_spec(
                    user_text,
                    messages=history or None,
                    username=username,
                    chat_id=chat_id,
                    images=images or None,
                )
            except ValueError as exc:
                yield f"[명세 생성 실패] {exc}"
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("vibe-spec failed")
                yield f"[명세 생성 오류] {exc}"
                return
            yield "```json\n"
            yield json.dumps(spec, ensure_ascii=False, indent=2)
            yield "\n```\n"
            return

        if mode == "code":
            try:
                spec = json.loads(user_text)
            except json.JSONDecodeError:
                yield (
                    "[vibe-code 사용법] 마지막 user 메시지에 spec JSON 본문만 넣어 보내세요. "
                    "(vibe-spec 으로 먼저 명세를 만들고 그 결과를 그대로 다음 호출의 user 메시지로 사용하세요.)"
                )
                return
            yield from pipeline.generate_code_from_spec(
                spec=spec,
                user_message="",
                messages=history or None,
                username=username,
                chat_id=chat_id,
            )
            return

        # mode == "auto"
        yield from pipeline.pipe(
            user_message=user_text,
            messages=history or None,
            username=username,
            chat_id=chat_id,
            images=images or None,
            skill_mode="auto",
        )

    if payload.stream:
        return StreamingResponse(
            stream_to_openai_sse(gen(), payload.model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Content-Encoding": "identity",
            },
        )

    return collect_to_chat_completion(gen(), payload.model)
