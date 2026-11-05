"""PDF 사이드카 HTTP 클라이언트.

Vibe 본체 → ``pdf-sidecar`` (포트 8102) WeasyPrint 컨테이너 호출.
사이드카는 ``X-Port-Project-Internal-Token`` 헤더로 인증.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

DEFAULT_SIDECAR_URL = "http://127.0.0.1:8102"
SIDECAR_TIMEOUT = 60.0


class PdfClientError(Exception):
    """공개용 PDF 사이드카 호출 실패."""


def _sidecar_url() -> str:
    return os.getenv("VIBE_PDF_SIDECAR_URL", DEFAULT_SIDECAR_URL).rstrip("/")


def _internal_token() -> str:
    token = os.getenv("VIBE_INTERNAL_TOKEN", "").strip()
    if not token:
        raise PdfClientError("VIBE_INTERNAL_TOKEN 이 설정되지 않았습니다 (사이드카 인증 불가).")
    return token


def is_available(timeout: float = 2.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{_sidecar_url()}/health")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


def render(
    source: str,
    kind: Literal["md", "html"],
    filename: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """``source`` 를 PDF 로 렌더하고 사이드카 응답(``{file_id, download_url, ...}``) 그대로 반환."""
    if kind == "md":
        endpoint = "/pdf/from-markdown"
        body = {"markdown": source}
    elif kind == "html":
        endpoint = "/pdf/from-html"
        body = {"html": source}
    else:
        raise PdfClientError(f"지원하지 않는 kind: {kind}")

    if filename:
        body["filename"] = filename
    if title:
        body["title"] = title

    headers = {
        "X-Port-Project-Internal-Token": _internal_token(),
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=SIDECAR_TIMEOUT) as client:
            resp = client.post(f"{_sidecar_url()}{endpoint}", json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise PdfClientError(f"사이드카 연결 실패: {exc}") from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except (ValueError, AttributeError):
            detail = resp.text
        raise PdfClientError(f"PDF 렌더 실패 (status {resp.status_code}): {detail}")

    try:
        return resp.json()
    except ValueError as exc:
        raise PdfClientError(f"사이드카 응답 파싱 실패: {exc}") from exc


def fetch_pdf_bytes(file_id: str, download: bool = True, timeout: float = 30.0) -> tuple[bytes, str]:
    """사이드카 ``/pdf/file/<id>`` 에서 실제 bytes 와 filename 가져오기.

    Vibe 본체는 다운로드 URL 을 자체 ``/downloads/<token>/<name>`` 으로 다시 발급하는
    것이 표준이므로 이 함수가 한 번 사이드카에서 bytes 를 받아 ``exports/`` 로 옮긴다.
    """
    url = f"{_sidecar_url()}/pdf/file/{file_id}"
    if download:
        url += "?download=1"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise PdfClientError(f"PDF 파일 다운로드 실패: {exc}") from exc

    disposition = resp.headers.get("content-disposition", "")
    filename = "document.pdf"
    if "filename=" in disposition:
        # 'attachment; filename="hello.pdf"' 같은 헤더 파싱
        try:
            after = disposition.split("filename=", 1)[1]
            filename = after.strip().strip('"').split(";", 1)[0].strip()
        except (IndexError, ValueError):
            pass
    return resp.content, filename
