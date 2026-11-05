"""OpenAPI tool server for Open WebUI.

This mounted app exposes Vibe tools with internal-token auth instead of the
browser session cookie used by ``/api/tools``. Open WebUI can import
``/tool-api/openapi.json`` and call these operations directly.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from pipelines import (
    download_tokens,
    pdf_client,
    preview_builder,
    text_export,
    tool_registry,
    web_extract,
)

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


def _clean_token(value: str | None) -> str:
    return (value or "").strip().lstrip("\ufeff")


def _internal_token() -> str:
    return _clean_token(
        os.getenv("PORT_PROJECT_INTERNAL_TOKEN", "") or os.getenv("VIBE_INTERNAL_TOKEN", "")
    )


def require_internal_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    x_port_project_internal_token: str | None = Header(
        default=None, alias="X-Port-Project-Internal-Token"
    ),
) -> None:
    expected = _internal_token()
    if not expected:
        return

    provided = ""
    if credentials and credentials.scheme.lower() == "bearer":
        provided = credentials.credentials
    elif x_port_project_internal_token:
        provided = x_port_project_internal_token

    import secrets

    if not provided or not secrets.compare_digest(_clean_token(provided), expected):
        raise HTTPException(status_code=401, detail="invalid internal token")


def _root_base_url(request: Request) -> str:
    configured = os.getenv("VIBE_PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")

    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    if forwarded_host:
        proto = forwarded_proto or request.url.scheme or "https"
        return f"{proto}://{forwarded_host}".rstrip("/")

    return f"{request.url.scheme}://{request.url.netloc}".rstrip("/")


def _download_urls(request: Request, token: str, filename: str) -> tuple[str, str]:
    encoded_name = quote(filename, safe="")
    preview = f"{_root_base_url(request)}/downloads/{token}/{encoded_name}"
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


router = APIRouter(dependencies=[Depends(require_internal_token)])


@router.get("/health", operation_id="vibe_tools_health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "vibe-tool-api"}


@router.get("/tools", operation_id="vibe_list_tools")
def list_tools() -> dict[str, Any]:
    return {"tools": tool_registry.list_enabled_tools(check_sidecars=True)}


@router.post("/web/fetch", response_model=WebFetchResponse, operation_id="vibe_web_fetch")
def web_fetch(payload: WebFetchRequest) -> WebFetchResponse:
    try:
        result = web_extract.fetch_as_markdown(payload.url)
    except web_extract.WebFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("tool-api web fetch unexpected error")
        raise HTTPException(status_code=502, detail=f"fetch 실패: {exc}") from exc

    return WebFetchResponse(
        url=result["url"],
        title=result.get("title", ""),
        markdown=result["markdown"],
        status_code=result.get("status_code", 0),
        char_count=result.get("char_count", 0),
        fetched_at=result["fetched_at"],
    )


@router.get("/text/formats", operation_id="vibe_text_formats")
def text_formats() -> dict[str, Any]:
    return {"formats": text_export.list_text_formats()}


@router.post("/text/export", response_model=ExportResponse, operation_id="vibe_text_export")
def export_text(payload: TextExportRequest, request: Request) -> ExportResponse:
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
        logger.exception("tool-api text export failed")
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
        summary=f"'{filename}' 파일을 만들었어요 ({size:,} bytes). {ttl // 60}분 동안 유효합니다.",
    )


@router.post("/pdf/render", response_model=ExportResponse, operation_id="vibe_pdf_render")
def render_pdf(payload: PdfRenderRequest, request: Request) -> ExportResponse:
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
        summary=f"'{safe_name}' PDF 를 만들었어요 ({len(pdf_bytes):,} bytes). {ttl // 60}분 동안 유효합니다.",
    )


@router.post("/code/preview", response_model=ExportResponse, operation_id="vibe_code_preview")
def code_preview(payload: CodePreviewRequest, request: Request) -> ExportResponse:
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


tool_app = FastAPI(
    title="Vibe Tool API",
    version="0.1.0",
    description="Open WebUI에서 호출할 Vibe 도구 서버입니다.",
    servers=[
        {
            "url": os.getenv(
                "VIBE_TOOL_API_PUBLIC_BASE_URL",
                "http://host.docker.internal:8100/tool-api",
            ).rstrip("/")
        }
    ],
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json",
    root_path_in_servers=False,
)
tool_app.include_router(router)
