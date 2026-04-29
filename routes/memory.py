"""User memory routes and helpers.

저장 형태: ``user_memory/<safe_username>.json`` 의 ``{items: [...]}``.
mtime 기반 캐시로 채팅 요청마다 발생하는 디스크 I/O 를 줄인다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .auth import require_session

ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = ROOT / "user_memory"

# 사용자당 메모리 항목 상한
MAX_MEMORY_ITEMS = 50


def memory_path(username: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", (username or "anonymous").strip()).strip("._") or "anonymous"
    MEMORY_DIR.mkdir(exist_ok=True)
    return MEMORY_DIR / f"{safe}.json"


# 메모리는 채팅 요청마다 읽히므로 mtime 기반 캐시로 디스크 I/O 를 줄인다.
_MEMORY_CACHE: dict[str, tuple[float, list[str]]] = {}


def load_memory(username: str) -> list[str]:
    path = memory_path(username)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cached = _MEMORY_CACHE.get(username)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("items", []) if isinstance(data, dict) else []
        cleaned = [str(x).strip() for x in items if str(x).strip()]
    except (json.JSONDecodeError, OSError):
        cleaned = []
    _MEMORY_CACHE[username] = (mtime, cleaned)
    return cleaned


def save_memory(username: str, items: list[str]) -> None:
    path = memory_path(username)
    path.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        _MEMORY_CACHE[username] = (path.stat().st_mtime, list(items))
    except OSError:
        _MEMORY_CACHE.pop(username, None)


def memory_block(username: str) -> str:
    """채팅 시스템 프롬프트에 주입할 메모리 텍스트 블록(없으면 빈 문자열)."""
    items = load_memory(username)
    if not items:
        return ""
    body = "\n".join(f"- {x}" for x in items)
    return f"[사용자가 기억해 두라고 한 사항]\n{body}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
router = APIRouter()


class MemoryPayload(BaseModel):
    items: list[str]


@router.get("/api/memory")
def get_memory(request: Request) -> dict[str, Any]:
    username = require_session(request)
    return {"items": load_memory(username)}


@router.put("/api/memory")
def put_memory(payload: MemoryPayload, request: Request) -> dict[str, bool]:
    username = require_session(request)
    cleaned = [str(x).strip() for x in payload.items if str(x).strip()][:MAX_MEMORY_ITEMS]
    save_memory(username, cleaned)
    return {"ok": True}
