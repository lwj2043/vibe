"""FastAPI backend for Vibe Coding chat UI.

- Serves chat_ui.html
- 라우트는 ``routes/`` 하위 모듈로 분리되어 있음:
    - routes/auth.py    : 로그인/세션/레이트리밋
    - routes/memory.py  : 사용자 메모리
    - routes/chat.py    : 채팅 + 채팅 로그 + 명세 단계 라우트
    - routes/spec.py    : 저장된 명세 조회

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

ROOT = Path(__file__).resolve().parent  # orchestrator/
PROJECT_ROOT = ROOT.parent              # vibe-rebuild/
DEPRECATED_DIR = PROJECT_ROOT / "deprecated"
CHAT_UI_PATH = DEPRECATED_DIR / "chat_ui.html"
ASSETS_DIR = DEPRECATED_DIR / "assets"
MAX_REQUEST_BYTES = int(os.getenv("VIBE_MAX_REQUEST_BYTES", str(25 * 1024 * 1024)))

# Allow `from pipelines.dual_model_pipeline import Pipeline`
sys.path.insert(0, str(ROOT))
from logging_setup import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger("server")

from routes import auth as auth_module  # noqa: E402
from routes import chat as chat_module  # noqa: E402
from routes import memory as memory_module  # noqa: E402
from routes import openai_compat as openai_compat_module  # noqa: E402
from routes import spec as spec_module  # noqa: E402
from routes import tool_api as tool_api_module  # noqa: E402
from routes import tools as tools_module  # noqa: E402


def _host_from_value(value: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if "://" in raw:
        parsed = urlparse(raw)
        return parsed.hostname
    return raw.split("/", 1)[0].strip()


def _allowed_hosts() -> list[str]:
    raw = os.getenv("VIBE_ALLOWED_HOSTS", "").strip()
    if raw:
        hosts = [_host_from_value(part) for part in raw.split(",")]
    else:
        hosts = [
            "localhost",
            "127.0.0.1",
            "[::1]",
            "host.docker.internal",
            "testserver",
            "vibeitz.com",
            "*.vibeitz.com",
            _host_from_value(os.getenv("VIBE_PUBLIC_HOSTNAME", "")),
            _host_from_value(os.getenv("VIBE_PUBLIC_BASE_URL", "")),
        ]
    return sorted({host for host in hosts if host})


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data: https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com https://esm.sh https://cdn.tailwindcss.com; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://unpkg.com https://esm.sh https://cdn.tailwindcss.com; "
        "connect-src 'self' https://cdn.jsdelivr.net https://unpkg.com https://esm.sh https://cdn.tailwindcss.com; "
        "frame-src 'self' data: blob:; "
        "child-src 'self' data: blob:; "
        "worker-src 'self' blob:"
    ),
}

app = FastAPI(title="Vibe Coding Service")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts())
# 스트리밍 응답은 GZipMiddleware 가 자동으로 패스스루한다.
# 일반 JSON/HTML 응답만 1KB 이상일 때 압축한다.


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "요청 본문이 너무 큽니다."},
                    headers={"Connection": "close"},
                )
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "잘못된 Content-Length 입니다."})

    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


app.include_router(auth_module.router)
app.include_router(memory_module.router)
app.include_router(chat_module.router)
app.include_router(spec_module.router)
app.include_router(tools_module.router)
app.include_router(tools_module.download_router)
# OpenAI 호환 /v1/* — Open WebUI 가 이 prefix 로 vibe 모델을 호출한다.
app.include_router(openai_compat_module.router)
# Open WebUI OpenAPI tool server. Import /tool-api/openapi.json from Open WebUI.
app.mount("/tool-api", tool_api_module.tool_app)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "vibe-orchestrator"}

# 정적 자산은 catch-all 라우트보다 먼저 마운트되어야 한다.
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        CHAT_UI_PATH,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_metadata() -> Response:
    return Response(status_code=204)


auth_module.ensure_default_user()


# ---------------------------------------------------------------------------
# 미리보기 iframe 정적 자산 처리 (style.css, script.js 404 방지)
# ---------------------------------------------------------------------------
# 생성된 HTML이 외부 파일(style.css, script.js 등)을 참조하면 미리보기 iframe이
# 현재 서버 오리진에서 해당 파일을 요청한다. 404 로그를 줄이기 위해 정적 자산
# 확장자로 보이는 경로는 빈 컨텐츠(204)로 조용히 응답한다.
_STATIC_PREVIEW_EXTS = (
    ".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf",
)


@app.get("/{asset_path:path}")
def preview_static_fallback(asset_path: str) -> Response:
    lowered = asset_path.lower()
    if lowered.endswith(_STATIC_PREVIEW_EXTS):
        if lowered.endswith(".css"):
            return Response(content="", media_type="text/css", status_code=204)
        if lowered.endswith((".js", ".mjs")):
            return Response(
                content="", media_type="application/javascript", status_code=204
            )
        return Response(status_code=204)
    raise HTTPException(status_code=404, detail="Not Found")


if __name__ == "__main__":
    logger.info("Vibe Coding server starting...")
    logger.info("Open in browser: http://127.0.0.1:8100")
    uvicorn.run("server:app", host="0.0.0.0", port=8100, reload=False)
