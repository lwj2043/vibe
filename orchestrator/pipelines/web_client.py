"""orchestrator → web-sidecar (8004) HTTP 클라이언트.

기존 ``pipelines/web_extract.py`` 는 fallback 로 남겨 두고, 평소엔 이 모듈로
sidecar 에 위임한다. sidecar 가 떠 있지 않거나 환경변수 설정이 비어 있으면
인라인 fetch 로 폴백한다.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from . import web_extract

logger = logging.getLogger(__name__)

WEB_SIDECAR_URL = os.getenv("WEB_SIDECAR_URL", "http://127.0.0.1:8104").rstrip("/")
WEB_SIDECAR_TIMEOUT = float(os.getenv("WEB_SIDECAR_TIMEOUT", "30"))
INTERNAL_TOKEN = os.getenv("PORT_PROJECT_INTERNAL_TOKEN", "") or os.getenv(
    "VIBE_INTERNAL_TOKEN", ""
)
USE_SIDECAR = os.getenv("VIBE_WEB_USE_SIDECAR", "true").lower() == "true"


def fetch_as_markdown(url: str, timeout: float | None = None) -> dict[str, Any]:
    """URL → {url, title, markdown, status_code, char_count, fetched_at, ...}.

    1차: web-sidecar(8004) 의 POST /web/fetch 에 위임.
    실패(연결 거부 / 5xx)하면 인라인 web_extract.fetch_as_markdown 로 폴백.
    """
    if USE_SIDECAR:
        try:
            return _fetch_via_sidecar(url, timeout)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("web-sidecar 호출 실패, 인라인으로 폴백: %s", exc)
        except web_extract.WebFetchError:
            # 사이드카가 4xx 로 의미 있는 에러를 돌려줬다면 그대로 위로 던진다.
            raise

    return web_extract.fetch_as_markdown(url, timeout=timeout or web_extract.DEFAULT_TIMEOUT)


def _fetch_via_sidecar(url: str, timeout: float | None) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if INTERNAL_TOKEN:
        headers["X-Port-Project-Internal-Token"] = INTERNAL_TOKEN

    payload: dict[str, Any] = {"url": url}
    if timeout:
        payload["timeout"] = timeout

    with httpx.Client(timeout=WEB_SIDECAR_TIMEOUT) as client:
        resp = client.post(
            f"{WEB_SIDECAR_URL}/web/fetch",
            json=payload,
            headers=headers,
        )

    if resp.status_code == 400:
        # 사이드카가 SSRF / DNS / 본문 추출 실패를 400 으로 돌려주는 케이스.
        detail = ""
        try:
            detail = (resp.json() or {}).get("detail", "")
        except ValueError:
            detail = resp.text
        raise web_extract.WebFetchError(detail or "web-sidecar fetch failed")

    if resp.status_code >= 500:
        # 사이드카 자체 장애 — 폴백 가능하도록 HTTPError 로 던진다.
        raise httpx.HTTPStatusError(
            f"web-sidecar 5xx: {resp.text[:200]}",
            request=resp.request,
            response=resp,
        )

    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("web-sidecar returned non-dict response")
    return data


def is_sidecar_alive() -> bool:
    """헬스체크 — start.ps1 또는 진단 도구에서 사용."""
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{WEB_SIDECAR_URL}/healthz")
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
