"""URL → Markdown sidecar (port 8004).

Brainrot 의 web-service 패턴: 외부 URL fetch + 본문 추출을 별도 프로세스로 격리.
- httpx + readability-lxml + markdownify 조합 (Playwright/Chromium 없음, 가벼움)
- SSRF 가드 (사설 IP / metadata IP 거부)
- UNTRUSTED_CONTENT_MARKER 로 LLM 프롬프트 인젝션 방어
- X-Port-Project-Internal-Token 으로 호출자(orchestrator/Open WebUI) 인증
"""
from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .extract import (
    DEFAULT_TIMEOUT,
    MAX_MARKDOWN_BYTES,
    WebFetchError,
    fetch_as_markdown,
)


def _clean_token(value: str | None) -> str:
    return (value or "").strip().lstrip("\ufeff")


INTERNAL_TOKEN = _clean_token(os.getenv("PORT_PROJECT_INTERNAL_TOKEN", ""))
PUBLIC_BASE_URL = os.getenv("WEB_SERVICE_PUBLIC_BASE_URL", "http://127.0.0.1:8104").rstrip("/")
MAX_FETCH_TIMEOUT = float(os.getenv("WEB_SERVICE_MAX_FETCH_TIMEOUT", "30"))


# /docs and the default /openapi.json are hidden — the spec is re-exposed below
# behind the internal-token gate so it can only be discovered by the orchestrator
# / Open WebUI tool registration flow.
app = FastAPI(
    title="Web Fetch (URL → Markdown)",
    version="0.1.0",
    servers=[{"url": PUBLIC_BASE_URL}],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _auth(token: str | None) -> None:
    """Constant-time check against PORT_PROJECT_INTERNAL_TOKEN.

    If the env is unset (e.g. local debugging), auth is skipped — but production
    start.ps1 always sets it so this branch only fires in dev.
    """
    if not INTERNAL_TOKEN:
        return
    if not token or not secrets.compare_digest(_clean_token(token), INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="invalid internal token")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class FetchRequest(BaseModel):
    url: str = Field(
        ...,
        description="가져올 외부 페이지 URL (http/https). 사설/루프백/메타데이터 IP 는 거부됩니다.",
        min_length=1,
        examples=["https://en.wikipedia.org/wiki/FastAPI"],
    )
    timeout: float | None = Field(
        None,
        description=f"개별 요청 타임아웃 (초). 기본 {DEFAULT_TIMEOUT}, 상한 {MAX_FETCH_TIMEOUT}.",
        gt=0,
    )


class FetchResponse(BaseModel):
    url: str = Field(..., description="redirect 까지 따라간 최종 URL")
    title: str = Field("", description="readability-lxml 가 추출한 제목 (없으면 빈 문자열)")
    author: str = Field("", description="추출 라이브러리 한계로 항상 빈 문자열")
    published: str = Field("", description="추출 라이브러리 한계로 항상 빈 문자열")
    markdown: str = Field(
        ...,
        description=(
            "본문 Markdown. 첫 줄에 UNTRUSTED_CONTENT_MARKER 가 포함되어 있으므로 "
            "LLM 호출 시 주석을 제거하지 말고 그대로 시스템 프롬프트에 주입할 것."
        ),
    )
    status_code: int = Field(..., description="원격 서버의 HTTP status (보통 200).")
    char_count: int = Field(..., description="UNTRUSTED_CONTENT_MARKER 를 제외한 본문 글자 수")
    fetched_at: str = Field(..., description="UTC ISO8601")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "web-sidecar", "max_markdown_bytes": MAX_MARKDOWN_BYTES}


# Brainrot 의 pdf-service 와 같은 방식: openapi 노출은 internal-token 뒤에.
@app.get("/openapi.json", include_in_schema=False)
def openapi_protected(
    x_port_project_internal_token: str | None = Header(
        default=None, alias="X-Port-Project-Internal-Token"
    ),
) -> dict[str, Any]:
    _auth(x_port_project_internal_token)
    return app.openapi()


@app.post(
    "/web/fetch",
    operation_id="web_fetch",
    summary="외부 URL 의 본문을 Markdown 으로 추출합니다.",
    response_model=FetchResponse,
)
def web_fetch(
    payload: FetchRequest,
    x_port_project_internal_token: str | None = Header(
        default=None, alias="X-Port-Project-Internal-Token"
    ),
) -> FetchResponse:
    _auth(x_port_project_internal_token)

    timeout = min(payload.timeout or DEFAULT_TIMEOUT, MAX_FETCH_TIMEOUT)
    try:
        result = fetch_as_markdown(payload.url, timeout=timeout)
    except WebFetchError as exc:
        # 클라이언트 친화 메시지 — 그대로 LLM 응답으로 흘려도 안전한 텍스트.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return FetchResponse(**result)
