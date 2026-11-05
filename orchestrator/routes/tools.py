"""도구(tool) 라우트.

Brainrot 의 마이크로서비스(web/text/pdf/...) 를 Vibe 본체에 통합한 결과:
- ``GET  /api/tools``                  활성 도구 목록
- ``POST /api/tools/web/fetch``        URL → Markdown
- ``POST /api/tools/text/export``      6포맷 export
- ``GET  /api/tools/text/formats``     지원 포맷 목록
- ``POST /api/tools/pdf/render``       MD/HTML → PDF (Phase 4)
- ``GET  /downloads/{token}/{name}``   TTL 보호 다운로드

모든 변형(mutating) 라우트는 ``require_session`` 으로 보호.
다운로드 라우트는 토큰 자체가 capability 이므로 별도 router 로 분리.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from pipelines import (
    download_tokens,
    pdf_client,
    preview_builder,
    text_export,
    tool_registry,
    web_extract,
)

from .auth import require_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tools")
download_router = APIRouter(prefix="/downloads")


def _public_base_url(request: Request) -> str:
    configured = os.getenv("VIBE_PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")

    public_hostname = os.getenv("VIBE_PUBLIC_HOSTNAME", "").strip()
    if public_hostname:
        if "://" not in public_hostname:
            public_hostname = f"https://{public_hostname}"
        return public_hostname.rstrip("/")

    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    if forwarded_host:
        proto = forwarded_proto or request.url.scheme or "https"
        return f"{proto}://{forwarded_host}".rstrip("/")

    return str(request.base_url).rstrip("/")


def _download_urls(request: Request, token: str, filename: str) -> tuple[str, str]:
    encoded_name = quote(filename, safe="")
    preview = f"{_public_base_url(request)}/downloads/{token}/{encoded_name}"
    return preview, f"{preview}?download=1"


def _issue_token_and_cleanup(path: Path) -> tuple[str, int]:
    source_dir = path.parent
    try:
        return download_tokens.issue_token(path)
    finally:
        try:
            source_dir.rmdir()
        except OSError:
            pass


class WebFetchRequest(BaseModel):
    url: str = Field(..., max_length=2048, description="가져올 페이지 URL (http/https)")


class WebFetchResponse(BaseModel):
    url: str
    title: str
    markdown: str
    status_code: int
    char_count: int
    fetched_at: str


@router.post("/web/fetch", response_model=WebFetchResponse)
def web_fetch_route(
    payload: WebFetchRequest,
    request: Request,
) -> WebFetchResponse:
    require_session(request)
    try:
        result = web_extract.fetch_as_markdown(payload.url)
    except web_extract.WebFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("web fetch unexpected error")
        raise HTTPException(status_code=502, detail=f"fetch 실패: {exc}") from exc

    return WebFetchResponse(
        url=result["url"],
        title=result.get("title", ""),
        markdown=result["markdown"],
        status_code=result.get("status_code", 0),
        char_count=result.get("char_count", 0),
        fetched_at=result["fetched_at"],
    )


# ---------------------------------------------------------------------------
# Text export
# ---------------------------------------------------------------------------
class TextExportRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=text_export.MAX_INPUT_BYTES)
    format: Literal["txt", "md", "html", "docx", "csv", "json"]
    filename: str | None = Field(None, max_length=120)
    title: str | None = Field(None, max_length=200)


class ExportResponse(BaseModel):
    file_id: str
    filename: str
    format: str
    size_bytes: int
    download_url: str
    preview_url: str
    expires_in_seconds: int
    expires_at: int
    summary: str


@router.get("/text/formats")
def text_formats(request: Request) -> dict[str, Any]:
    require_session(request)
    return {"formats": text_export.list_text_formats()}


@router.post("/text/export", response_model=ExportResponse)
def text_export_route(payload: TextExportRequest, request: Request) -> ExportResponse:
    require_session(request)

    base = (payload.filename or "document").strip() or "document"
    try:
        produced = text_export.write_text_file(
            content=payload.content,
            format=payload.format,
            filename_base=base,
            title=payload.title,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("text export failed")
        raise HTTPException(status_code=500, detail=f"export 실패: {exc}") from exc

    size = produced.stat().st_size
    token, expires_at = _issue_token_and_cleanup(produced)
    final_path = download_tokens.EXPORTS_DIR / token / produced.name
    filename = final_path.name

    preview, download = _download_urls(request, token, filename)
    ttl = download_tokens.DEFAULT_TTL_SECONDS

    return ExportResponse(
        file_id=token,
        filename=filename,
        format=payload.format,
        size_bytes=size,
        download_url=download,
        preview_url=preview,
        expires_in_seconds=ttl,
        expires_at=expires_at,
        summary=(
            f"'{filename}' 파일을 만들었어요 ({size:,} bytes). "
            f"{ttl // 60}분 동안 유효합니다."
        ),
    )


# ---------------------------------------------------------------------------
# PDF (사이드카)
# ---------------------------------------------------------------------------
class PdfRenderRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=2 * 1024 * 1024)
    kind: Literal["md", "html"]
    filename: str | None = Field(None, max_length=120)
    title: str | None = Field(None, max_length=200)


class CodePreviewRequest(BaseModel):
    code_output: str | None = Field(
        None,
        max_length=2 * 1024 * 1024,
        description="```index.html ...``` 형식의 코드 생성 결과 전체",
    )
    files: dict[str, str] | None = Field(
        None,
        description="파일 경로를 키로 갖는 코드 맵. index.html, CSS, JS를 지원합니다.",
    )
    html: str | None = Field(None, max_length=2 * 1024 * 1024, description="단일 HTML 본문")
    filename: str | None = Field(None, max_length=120)


@router.post("/pdf/render", response_model=ExportResponse)
def pdf_render_route(payload: PdfRenderRequest, request: Request) -> ExportResponse:
    require_session(request)
    if not pdf_client.is_available():
        raise HTTPException(status_code=503, detail="PDF 사이드카가 기동되어 있지 않습니다.")

    try:
        sidecar_resp = pdf_client.render(
            source=payload.source,
            kind=payload.kind,
            filename=payload.filename,
            title=payload.title,
        )
        pdf_bytes, filename = pdf_client.fetch_pdf_bytes(sidecar_resp["file_id"], download=True)
    except pdf_client.PdfClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=502, detail=f"사이드카 응답에 file_id 없음: {exc}") from exc

    safe_name = download_tokens.sanitize_filename(filename, default_stem="document")
    if not safe_name.lower().endswith(".pdf"):
        safe_name = f"{safe_name}.pdf"

    tmp_dir = Path(tempfile.mkdtemp(prefix="vibe-pdf-"))
    tmp_path = tmp_dir / safe_name
    tmp_path.write_bytes(pdf_bytes)

    token, expires_at = _issue_token_and_cleanup(tmp_path)
    preview, download = _download_urls(request, token, safe_name)
    ttl = download_tokens.DEFAULT_TTL_SECONDS

    return ExportResponse(
        file_id=token,
        filename=safe_name,
        format="pdf",
        size_bytes=len(pdf_bytes),
        download_url=download,
        preview_url=preview,
        expires_in_seconds=ttl,
        expires_at=expires_at,
        summary=(
            f"'{safe_name}' PDF 를 만들었어요 ({len(pdf_bytes):,} bytes). "
            f"{ttl // 60}분 동안 유효합니다."
        ),
    )


# ---------------------------------------------------------------------------
# Code preview
# ---------------------------------------------------------------------------
@router.post("/code/preview", response_model=ExportResponse)
def code_preview_route(payload: CodePreviewRequest, request: Request) -> ExportResponse:
    require_session(request)

    files: dict[str, str] = {}
    if payload.code_output:
        files.update(preview_builder.extract_code_blocks(payload.code_output))
    if payload.files:
        files.update(payload.files)
    if payload.html:
        files.setdefault("index.html", payload.html)

    combined = preview_builder.build_combined_html(files)
    if not combined:
        raise HTTPException(
            status_code=400,
            detail="프리뷰를 만들 HTML 엔트리가 없습니다. index.html 또는 html 본문을 보내주세요.",
        )

    safe_name = download_tokens.sanitize_filename(payload.filename, default_stem="code-preview")
    if not safe_name.lower().endswith((".html", ".htm")):
        safe_name = f"{safe_name}.html"

    tmp_dir = Path(tempfile.mkdtemp(prefix="vibe-code-preview-"))
    tmp_path = tmp_dir / safe_name
    tmp_path.write_text(combined, encoding="utf-8")

    token, expires_at = _issue_token_and_cleanup(tmp_path)
    preview, download = _download_urls(request, token, safe_name)
    ttl = download_tokens.DEFAULT_TTL_SECONDS

    return ExportResponse(
        file_id=token,
        filename=safe_name,
        format="html",
        size_bytes=len(combined.encode("utf-8")),
        download_url=download,
        preview_url=preview,
        expires_in_seconds=ttl,
        expires_at=expires_at,
        summary=f"코드 프리뷰 '{safe_name}'를 만들었어요. {ttl // 60}분 동안 유효합니다.",
    )


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
@router.get("")
def list_tools(request: Request, check: int = Query(0, description="1 이면 사이드카 health 체크")) -> dict[str, Any]:
    """``tools/registry.json`` 기반 활성 도구 목록."""
    require_session(request)
    return {"tools": tool_registry.list_enabled_tools(check_sidecars=bool(check))}


# ---------------------------------------------------------------------------
# Downloads (capability URL: token 자체가 인증)
# ---------------------------------------------------------------------------
_PDF_TYPES = {"pdf": "application/pdf"}
_TEXT_TYPES = {
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@download_router.get("/{token}/{filename}")
def download_file(
    token: str,
    filename: str,
    download: int = Query(0, description="1 이면 attachment 헤더로 강제 다운로드."),
) -> FileResponse:
    path = download_tokens.resolve_token(token, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="not found or expired")

    ext = path.suffix.lstrip(".").lower()
    media_type = _TEXT_TYPES.get(ext) or _PDF_TYPES.get(ext) or "application/octet-stream"
    safe_name = path.name.replace("\r", "").replace("\n", "")
    disposition = "attachment" if download else "inline"

    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=safe_name,
        content_disposition_type=disposition,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "private, no-store",
        },
    )
