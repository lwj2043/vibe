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
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
CHAT_UI_PATH = ROOT / "chat_ui.html"
ASSETS_DIR = ROOT / "assets"

# Allow `from pipelines.dual_model_pipeline import Pipeline`
sys.path.insert(0, str(ROOT))
from logging_setup import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger("server")

from routes import auth as auth_module  # noqa: E402
from routes import chat as chat_module  # noqa: E402
from routes import memory as memory_module  # noqa: E402
from routes import spec as spec_module  # noqa: E402

app = FastAPI(title="Vibe Coding Service")
# 스트리밍 응답은 GZipMiddleware 가 자동으로 패스스루한다.
# 일반 JSON/HTML 응답만 1KB 이상일 때 압축한다.
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(auth_module.router)
app.include_router(memory_module.router)
app.include_router(chat_module.router)
app.include_router(spec_module.router)

# 정적 자산은 catch-all 라우트보다 먼저 마운트되어야 한다.
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        CHAT_UI_PATH,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


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
    logger.info("Open in browser: http://127.0.0.1:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
