"""Markdown / HTML → PDF rendering tool.

Uses WeasyPrint to render HTML to PDF; Markdown input is first converted to HTML
via the `markdown` library with code highlighting and tables enabled. Generated
PDFs live in /data and are reachable via capability URLs (random uuid id) for
TTL seconds.
"""
from __future__ import annotations

import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


def _clean_token(value: str | None) -> str:
    return (value or "").strip().lstrip("\ufeff")


INTERNAL_TOKEN = _clean_token(os.getenv("PORT_PROJECT_INTERNAL_TOKEN", ""))
PUBLIC_BASE_URL = os.getenv("PDF_SERVICE_PUBLIC_BASE_URL", "http://127.0.0.1:8102").rstrip("/")
DATA_DIR = Path(os.getenv("PDF_SERVICE_DATA_DIR", "/data"))
TTL_SECONDS = int(os.getenv("PORT_PROJECT_DOWNLOAD_TOKEN_TTL_SECONDS", "3600"))
MAX_INPUT_BYTES = int(os.getenv("PDF_SERVICE_MAX_INPUT_BYTES", str(2 * 1024 * 1024)))  # 2 MB

DATA_DIR.mkdir(parents=True, exist_ok=True)

# Hide /docs and /openapi.json from default routes — Open WebUI fetches the spec
# via /openapi.json (which we re-add behind the internal-token gate below).
# Anonymous Swagger UI / spec leaks are unnecessary attack-surface info.
app = FastAPI(
    title="Markdown / HTML to PDF",
    version="0.1.0",
    servers=[{"url": PUBLIC_BASE_URL}],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

SAFE_NAME = re.compile(r"^[A-Za-z0-9._\- 가-힣]+$")

# CSS used as a default print stylesheet for both Markdown and bare HTML inputs.
DEFAULT_PRINT_CSS = """
@page { size: A4; margin: 18mm 16mm; }
html { font-family: "Noto Sans CJK KR", "Noto Sans", sans-serif; font-size: 11pt; line-height: 1.55; }
body { color: #1a1a1a; }
h1, h2, h3, h4, h5, h6 { font-weight: 600; margin-top: 1.4em; margin-bottom: 0.4em; }
h1 { font-size: 1.9em; border-bottom: 2px solid #ddd; padding-bottom: 0.2em; }
h2 { font-size: 1.5em; border-bottom: 1px solid #eee; padding-bottom: 0.15em; }
h3 { font-size: 1.25em; }
p { margin: 0.5em 0; }
ul, ol { margin: 0.4em 0 0.4em 1.6em; padding: 0; }
li { margin: 0.15em 0; }
code { font-family: "Noto Sans Mono CJK KR", "JetBrains Mono", "Source Code Pro", monospace;
       background: #f4f4f4; border-radius: 3px; padding: 0.05em 0.3em; font-size: 0.92em; }
pre { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px;
      padding: 12px; overflow-x: auto; font-size: 0.88em; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #dfe2e5; color: #555; padding: 0 1em; margin: 0.6em 0; }
table { border-collapse: collapse; width: 100%; margin: 0.6em 0; }
th, td { border: 1px solid #d0d7de; padding: 6px 10px; text-align: left; }
th { background: #f6f8fa; font-weight: 600; }
hr { border: none; border-top: 1px solid #ddd; margin: 1em 0; }
img { max-width: 100%; }
a { color: #0969da; text-decoration: none; }
"""


def _auth(token: str | None) -> None:
    if not INTERNAL_TOKEN:
        return
    if not token or not secrets.compare_digest(_clean_token(token), INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="invalid internal token")


def _sanitize_filename(name: str | None) -> str:
    name = (name or "").strip() or "document"
    name = name.removesuffix(".pdf")
    if not SAFE_NAME.match(name):
        name = re.sub(r"[^A-Za-z0-9._\- 가-힣]", "_", name)
    if len(name) > 75:
        name = name[:75]
    return f"{name}.pdf"


def _markdown_to_html(md: str, title: str) -> str:
    import markdown as md_lib
    body = md_lib.markdown(
        md,
        extensions=["extra", "sane_lists", "tables", "fenced_code", "codehilite", "toc"],
        extension_configs={"codehilite": {"guess_lang": False, "css_class": "codehilite"}},
        output_format="html5",
    )
    return _wrap_html(body, title=title, include_codehilite_css=True)


def _wrap_html(body: str, *, title: str, include_codehilite_css: bool) -> str:
    extra_css = ""
    if include_codehilite_css:
        try:
            from pygments.formatters import HtmlFormatter
            extra_css = HtmlFormatter(style="default").get_style_defs(".codehilite")
        except Exception:
            extra_css = ""
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>{_escape(title)}</title>"
        f"<style>{DEFAULT_PRINT_CSS}\n{extra_css}</style>"
        "</head><body>"
        f"{body}"
        "</body></html>"
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")
    )


def _safe_url_fetcher(url: str) -> dict:
    """SSRF guard for WeasyPrint. Blocks every non-data: URL — including
    http(s):, file:, ftp:, gopher:. Without this, an LLM (or chat user) could
    submit `<img src=http://internal-vm/secret>` and trigger an outbound fetch
    from the pdf-service container to internal services. data: URIs (inline
    base64 images) stay allowed because they involve no network I/O.
    """
    if url.startswith("data:"):
        from weasyprint.urls import default_url_fetcher
        return default_url_fetcher(url)
    raise ValueError(f"외부 URL 로딩 차단: {url[:120]}")


def _render_pdf(html: str) -> bytes:
    from weasyprint import HTML
    return HTML(string=html, base_url=str(DATA_DIR), url_fetcher=_safe_url_fetcher).write_pdf()


def _expire_old_files() -> None:
    cutoff = time.time() - TTL_SECONDS
    for path in DATA_DIR.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _resolve_file(file_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", file_id):
        raise HTTPException(status_code=404, detail="not found")
    path = DATA_DIR / f"{file_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found or expired")
    if time.time() - path.stat().st_mtime > TTL_SECONDS:
        try:
            path.unlink()
        except OSError:
            pass
        raise HTTPException(status_code=410, detail="expired")
    return path


def _save_pdf(pdf_bytes: bytes, friendly_name: str) -> tuple[str, Path]:
    file_id = uuid.uuid4().hex
    path = DATA_DIR / f"{file_id}.pdf"
    path.write_bytes(pdf_bytes)
    name_path = DATA_DIR / f"{file_id}.name"
    name_path.write_text(friendly_name, encoding="utf-8")
    # 0600: only the service user can read these on the host bind/volume mount.
    # Any other UID inside the same volume (sibling container, host accounts)
    # cannot fish out files by guessing UUIDs without the capability URL.
    try:
        path.chmod(0o600)
        name_path.chmod(0o600)
    except OSError:
        pass
    return file_id, path


def _build_response(file_id: str, pdf_bytes: bytes, friendly_name: str) -> "RenderPdfResponse":
    base = f"{PUBLIC_BASE_URL}/pdf/file/{file_id}"
    return RenderPdfResponse(
        file_id=file_id,
        filename=friendly_name,
        size_bytes=len(pdf_bytes),
        download_url=f"{base}?download=1",
        preview_url=base,
        expires_in_seconds=TTL_SECONDS,
        summary=(
            f"'{friendly_name}' PDF 를 만들었어요 ({len(pdf_bytes):,} bytes). "
            f"브라우저에서 열려면 {base}, 다운로드는 {base}?download=1. "
            f"링크는 {TTL_SECONDS // 60}분 동안 유효합니다."
        ),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/openapi.json", include_in_schema=False)
def openapi_protected(
    x_port_project_internal_token: str | None = Header(default=None, alias="X-Port-Project-Internal-Token"),
) -> dict[str, Any]:
    _auth(x_port_project_internal_token)
    return app.openapi()


class RenderPdfResponse(BaseModel):
    file_id: str
    filename: str
    size_bytes: int
    download_url: str = Field(..., description="브라우저에서 클릭 시 다운로드 강제.")
    preview_url: str = Field(..., description="브라우저에서 클릭 시 inline PDF 뷰어로 렌더.")
    expires_in_seconds: int
    summary: str = Field(..., description="모델이 사용자에게 그대로 전달할 한국어 요약.")


class MarkdownToPdfRequest(BaseModel):
    markdown: str = Field(
        ...,
        description="GFM 호환 Markdown 본문. 코드블록, 테이블, 체크리스트 모두 지원.",
        min_length=1,
    )
    filename: str | None = Field(
        None,
        description="결과 PDF 파일 이름 (확장자 없이도 됨). 미지정이면 'document'.",
        examples=["weekly_report", "주간 보고"],
    )
    title: str | None = Field(
        None,
        description="HTML title 태그에 들어갈 제목. 미지정이면 filename 사용.",
    )


@app.post(
    "/pdf/from-markdown",
    operation_id="markdown_to_pdf",
    summary="Markdown 본문을 PDF 로 렌더하고 다운로드/프리뷰 URL 을 반환합니다.",
    response_model=RenderPdfResponse,
)
def markdown_to_pdf(
    payload: MarkdownToPdfRequest,
    x_port_project_internal_token: str | None = Header(default=None, alias="X-Port-Project-Internal-Token"),
) -> RenderPdfResponse:
    _auth(x_port_project_internal_token)
    if len(payload.markdown.encode("utf-8")) > MAX_INPUT_BYTES:
        raise HTTPException(status_code=413, detail=f"markdown too large (>{MAX_INPUT_BYTES} bytes)")

    friendly = _sanitize_filename(payload.filename)
    title = (payload.title or friendly.removesuffix(".pdf")).strip() or "document"
    html = _markdown_to_html(payload.markdown, title=title)
    try:
        pdf_bytes = _render_pdf(html)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"PDF render failed: {e}") from e

    _expire_old_files()
    file_id, _ = _save_pdf(pdf_bytes, friendly)
    return _build_response(file_id, pdf_bytes, friendly)


class HtmlToPdfRequest(BaseModel):
    html: str = Field(
        ...,
        description=(
            "PDF 로 변환할 HTML 본문. 완전한 <html> 문서거나, 그냥 <body> 안에 들어갈 조각이어도 됩니다 "
            "(조각이면 자동으로 head/style 을 감싸 렌더). 외부 리소스는 가져오지 않습니다."
        ),
        min_length=1,
    )
    filename: str | None = Field(None, description="결과 PDF 파일 이름.")
    title: str | None = Field(None, description="HTML title 태그.")


@app.post(
    "/pdf/from-html",
    operation_id="html_to_pdf",
    summary="HTML 문서/조각을 PDF 로 렌더하고 다운로드/프리뷰 URL 을 반환합니다.",
    response_model=RenderPdfResponse,
)
def html_to_pdf(
    payload: HtmlToPdfRequest,
    x_port_project_internal_token: str | None = Header(default=None, alias="X-Port-Project-Internal-Token"),
) -> RenderPdfResponse:
    _auth(x_port_project_internal_token)
    if len(payload.html.encode("utf-8")) > MAX_INPUT_BYTES:
        raise HTTPException(status_code=413, detail=f"html too large (>{MAX_INPUT_BYTES} bytes)")

    friendly = _sanitize_filename(payload.filename)
    title = (payload.title or friendly.removesuffix(".pdf")).strip() or "document"

    body = payload.html.strip()
    full_html = body if "<html" in body.lower() else _wrap_html(body, title=title, include_codehilite_css=False)

    try:
        pdf_bytes = _render_pdf(full_html)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"PDF render failed: {e}") from e

    _expire_old_files()
    file_id, _ = _save_pdf(pdf_bytes, friendly)
    return _build_response(file_id, pdf_bytes, friendly)


@app.get(
    "/pdf/file/{file_id}",
    operation_id="pdf_file_serve",
    summary="생성된 PDF 다운로드 / 프리뷰. ?download=1 이면 첨부 헤더로 강제 다운로드.",
    include_in_schema=False,
)
def serve_pdf(
    file_id: str,
    download: int = Query(0, description="1 이면 attachment 헤더로 강제 다운로드."),
) -> FileResponse:
    path = _resolve_file(file_id)
    name_path = DATA_DIR / f"{file_id}.name"
    raw_name = name_path.read_text(encoding="utf-8").strip() if name_path.exists() else path.name
    # Defense in depth: filename is sanitized at write time, but strip CR/LF here
    # too so a corrupted .name file can never inject extra response headers via
    # Content-Disposition.
    filename = raw_name.replace("\r", "").replace("\n", "")
    if not SAFE_NAME.match(filename.removesuffix(".pdf")):
        filename = "document.pdf"

    safety_headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
        "Referrer-Policy": "no-referrer",
    }
    if download:
        return FileResponse(
            path=str(path),
            media_type="application/pdf",
            filename=filename,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                **safety_headers,
            },
        )
    # inline (browser PDF viewer)
    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=filename,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            **safety_headers,
        },
    )


@app.get(
    "/pdf/info",
    operation_id="pdf_service_info",
    summary="PDF 도구의 입력 제한과 TTL 정보.",
)
def info(
    x_port_project_internal_token: str | None = Header(default=None, alias="X-Port-Project-Internal-Token"),
) -> dict[str, Any]:
    _auth(x_port_project_internal_token)
    return {
        "max_input_bytes": MAX_INPUT_BYTES,
        "ttl_seconds": TTL_SECONDS,
        "engines": {"markdown": "python-markdown + weasyprint", "html": "weasyprint"},
    }
