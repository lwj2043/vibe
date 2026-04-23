"""FastAPI backend for Vibe Coding chat UI.

- Serves chat_ui.html
- /api/login: validates credentials against users.json (PBKDF2-hashed)
- /api/chat: streams pipeline output back to the browser

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
USERS_PATH = ROOT / "users.json"
CHAT_LOGS_DIR = ROOT / "chat_logs"
CHAT_UI_PATH = ROOT / "chat_ui.html"
MEMORY_DIR = ROOT / "user_memory"

# Allow `from pipelines.dual_model_pipeline import Pipeline`
sys.path.insert(0, str(ROOT))
from pipelines.dual_model_pipeline import Pipeline  # noqa: E402
from pipelines.spec_storage import spec_dir as _spec_dir  # noqa: E402  # sanitized path helper

app = FastAPI(title="Vibe Coding Service")
pipeline = Pipeline()

# token -> username
SESSIONS: dict[str, str] = {}

PBKDF2_ITERS = 200_000


# ---------------------------------------------------------------------------
# users.json helpers
# ---------------------------------------------------------------------------
def _hash_password(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERS
    )
    return dk.hex()


def _make_password_hash(password: str) -> str:
    """Return 'salt:hash' string (no separate salt field needed)."""
    salt = secrets.token_hex(16)
    return f"{salt}:{_hash_password(password, salt)}"


def _load_users() -> list[dict[str, Any]]:
    if not USERS_PATH.exists():
        return []
    try:
        data = json.loads(USERS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("users", []) if isinstance(data, dict) else []


def _save_users(users: list[dict[str, Any]]) -> None:
    USERS_PATH.write_text(
        json.dumps({"users": users}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _ensure_default_user() -> None:
    """Create users.json with a default admin/admin account if missing."""
    if USERS_PATH.exists():
        return
    _save_users([{"username": "admin", "password_hash": _make_password_hash("admin")}])


def _verify_credentials(username: str, password: str) -> bool:
    for user in _load_users():
        if user.get("username") != username:
            continue
        stored = user.get("password_hash", "")
        if not stored:
            return False
        # Support legacy format (separate salt field) and new format (salt:hash)
        if ":" in stored:
            salt, expected = stored.split(":", 1)
        else:
            salt = user.get("salt", "")
            expected = stored
        if not salt or not expected:
            return False
        candidate = _hash_password(password, salt)
        return secrets.compare_digest(candidate, expected)
    return False


def _require_session(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if not token or token not in SESSIONS:
        raise HTTPException(status_code=401, detail="인증이 필요합니다")
    return SESSIONS[token]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        CHAT_UI_PATH,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


class LoginPayload(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(payload: LoginPayload) -> dict[str, str]:
    if not _verify_credentials(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = payload.username
    return {"token": token, "username": payload.username}


@app.post("/api/logout")
def logout(request: Request) -> dict[str, bool]:
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    SESSIONS.pop(token, None)
    return {"ok": True}


class RegisterPayload(BaseModel):
    username: str
    password: str


@app.post("/api/register")
def register(payload: RegisterPayload) -> dict[str, str]:
    username = payload.username.strip()
    if not username or not payload.password:
        raise HTTPException(status_code=400, detail="아이디와 비밀번호를 입력해주세요")
    if len(username) < 2:
        raise HTTPException(status_code=400, detail="아이디는 2자 이상이어야 합니다")
    if len(payload.password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다")
    users = _load_users()
    if any(u.get("username") == username for u in users):
        raise HTTPException(status_code=409, detail="이미 존재하는 아이디입니다")
    users.append({"username": username, "password_hash": _make_password_hash(payload.password)})
    _save_users(users)
    return {"ok": "registered"}


# ---------------------------------------------------------------------------
# Chat log persistence (per-user)
# ---------------------------------------------------------------------------
# 저장 구조:
#   chat_logs/
#     <username>/
#       state.json   — 프런트 복원용(chats + settings, 기존 구조 유지)
#       log.jsonl    — 턴 단위 감사 로그. 한 줄에 {date,time,input,response}
def _user_log_dir(username: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", (username or "anonymous").strip()).strip("._") or "anonymous"
    path = CHAT_LOGS_DIR / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path(username: str) -> Path:
    return _user_log_dir(username) / "state.json"


def _jsonl_path(username: str) -> Path:
    return _user_log_dir(username) / "log.jsonl"


def _legacy_single_file_path(username: str) -> Path:
    return CHAT_LOGS_DIR / f"{username}.json"


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    return content if isinstance(content, str) else str(content)


def _split_date_time(timestamp_ms: Any) -> tuple[str, str]:
    """ms 타임스탬프 → (date, time) 문자열. 실패 시 현재 시각 사용."""
    try:
        if isinstance(timestamp_ms, (int, float)):
            dt = datetime.fromtimestamp(timestamp_ms / 1000).astimezone()
        else:
            dt = datetime.now().astimezone()
    except (OverflowError, OSError, ValueError):
        dt = datetime.now().astimezone()
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")


def _build_simple_logs(chats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """모든 채팅의 (user → assistant) 턴을 단순 로그 엔트리로 변환.

    각 엔트리는 {date, time, input, response} 형식만 가집니다.
    (user 는 파일 경로로 식별되므로 본문에 넣지 않습니다.)
    """
    logs: list[dict[str, Any]] = []
    for chat in chats:
        if not isinstance(chat, dict):
            continue
        pending_user: dict[str, Any] | None = None
        for message in chat.get("messages", []) or []:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "user":
                pending_user = message
                continue
            if role != "assistant" or pending_user is None:
                continue
            date_str, time_str = _split_date_time(message.get("timestamp"))
            logs.append(
                {
                    "date": date_str,
                    "time": time_str,
                    "input": _message_text(pending_user),
                    "response": _message_text(message),
                }
            )
            pending_user = None
    return logs


def _sanitize_settings(settings: dict[str, Any] | None) -> dict[str, Any] | None:
    if settings is None:
        return None

    sanitized = dict(settings)
    sanitized.pop("defaultModel", None)
    return sanitized


def _sanitize_message(message: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(message)
    sanitized.pop("timestamp", None)
    sanitized.pop("createdAt", None)
    if sanitized.get("role") != "assistant" or sanitized.get("durationMs") is None:
        sanitized.pop("durationMs", None)
    return sanitized


def _sanitize_chat(chat: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(chat)
    messages = chat.get("messages", [])
    if isinstance(messages, list):
        sanitized["messages"] = [
            _sanitize_message(message) for message in messages if isinstance(message, dict)
        ]
    else:
        sanitized["messages"] = []
    return sanitized


@app.get("/api/chats")
def get_chats(request: Request) -> dict:
    username = _require_session(request)
    state = _state_path(username)
    # 새 포맷 우선
    if state.exists():
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            return {
                "chats": data.get("chats", []),
                "settings": data.get("settings"),
            }
        except (json.JSONDecodeError, OSError):
            return {"chats": [], "settings": None}
    # 구 포맷(단일 파일) 마이그레이션 — 읽기 전용 폴백
    legacy = _legacy_single_file_path(username)
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            return {
                "chats": data.get("chats", []),
                "settings": data.get("settings"),
            }
        except (json.JSONDecodeError, OSError):
            pass
    return {"chats": [], "settings": None}


class SaveChatsPayload(BaseModel):
    chats: list
    settings: dict | None = None


@app.put("/api/chats")
def save_chats(payload: SaveChatsPayload, request: Request) -> dict:
    username = _require_session(request)
    CHAT_LOGS_DIR.mkdir(exist_ok=True)

    settings = _sanitize_settings(payload.settings)
    # 원본(타임스탬프 포함) chats 로 먼저 감사 로그를 만든 뒤, sanitize 된 버전은
    # 복원용 state.json 에만 사용한다.
    raw_chats = [chat for chat in payload.chats if isinstance(chat, dict)]
    chats = [_sanitize_chat(chat) for chat in raw_chats]

    # 1) 복원용 상태 파일 — chat_logs/<user>/state.json
    state_path = _state_path(username)
    state_path.write_text(
        json.dumps(
            {"chats": chats, "settings": settings},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 2) 턴 단위 감사 로그 — chat_logs/<user>/log.jsonl
    #    한 줄당 {"date","time","input","response"} 만 담음.
    #    append-only: 이미 기록된 턴은 건너뛰고 새 턴만 끝에 추가해서
    #    날짜/시간 정보가 덮어써지지 않도록 한다.
    logs = _build_simple_logs(raw_chats)
    jsonl_path = _jsonl_path(username)

    seen: set[tuple[str, str]] = set()
    if jsonl_path.exists():
        try:
            for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    seen.add((str(rec.get("input", "")), str(rec.get("response", ""))))
        except OSError:
            seen = set()

    new_entries: list[dict[str, Any]] = []
    for entry in logs:
        key = (str(entry.get("input", "")), str(entry.get("response", "")))
        if key in seen:
            continue
        seen.add(key)
        new_entries.append(entry)

    if new_entries:
        with jsonl_path.open("a", encoding="utf-8") as f:
            for entry in new_entries:
                f.write(json.dumps(entry, ensure_ascii=False))
                f.write("\n")

    # 3) 구 단일 파일은 더 이상 쓰지 않음 — 있으면 조용히 제거하여 혼란 방지
    legacy = _legacy_single_file_path(username)
    if legacy.exists():
        try:
            legacy.unlink()
        except OSError:
            pass

    return {"ok": True}


class ChatPayload(BaseModel):
    message: str
    chat_id: str | None = None
    messages: list[dict[str, Any]] | None = None
    attachments: list[dict[str, Any]] | None = None


def _parse_attachments(
    attachments: list[dict[str, Any]] | None,
) -> tuple[list[bytes], str]:
    """클라이언트가 보낸 attachments 배열을 (이미지 바이트 목록, 텍스트 컨텍스트) 로 분해.

    - 이미지(image/*): 바이너리로 디코드해 images 로 돌려줌
    - PDF(application/pdf): 텍스트 추출 시도 → context 에 '[파일: 이름]\\n...' 형식으로 누적
    - 그 외: 무시
    """
    images: list[bytes] = []
    text_parts: list[str] = []
    if not attachments:
        return images, ""
    for att in attachments:
        if not isinstance(att, dict):
            continue
        data = att.get("data") or ""
        name = att.get("name") or "(이름 없음)"
        mime = (att.get("type") or "").lower()
        if not isinstance(data, str) or "," not in data:
            continue
        try:
            raw = base64.b64decode(data.split(",", 1)[1])
        except Exception:  # noqa: BLE001
            continue
        if mime.startswith("image/"):
            images.append(raw)
        elif mime == "application/pdf" or name.lower().endswith(".pdf"):
            extracted = _extract_pdf_text(raw)
            if extracted:
                text_parts.append(f"[첨부 파일: {name}]\n{extracted}")
    return images, ("\n\n".join(text_parts)).strip()


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """PDF 바이트에서 텍스트를 추출. 의존성이 없거나 실패하면 빈 문자열."""
    try:
        from pypdf import PdfReader
        from io import BytesIO
    except ImportError:
        return ""
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                pages.append("")
        text = "\n".join(pages).strip()
        if len(text) > 20000:
            text = text[:20000] + "\n… (이후 내용 생략)"
        return text
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# User memory (persistent facts injected into every chat)
# ---------------------------------------------------------------------------
def _memory_path(username: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", (username or "anonymous").strip()).strip("._") or "anonymous"
    MEMORY_DIR.mkdir(exist_ok=True)
    return MEMORY_DIR / f"{safe}.json"


# 메모리는 채팅 요청마다 읽히므로 mtime 기반 캐시로 디스크 I/O 를 줄인다.
_MEMORY_CACHE: dict[str, tuple[float, list[str]]] = {}


def _load_memory(username: str) -> list[str]:
    path = _memory_path(username)
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


def _save_memory(username: str, items: list[str]) -> None:
    path = _memory_path(username)
    path.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        _MEMORY_CACHE[username] = (path.stat().st_mtime, list(items))
    except OSError:
        _MEMORY_CACHE.pop(username, None)


def _memory_block(username: str) -> str:
    items = _load_memory(username)
    if not items:
        return ""
    body = "\n".join(f"- {x}" for x in items)
    return f"[사용자가 기억해 두라고 한 사항]\n{body}"


@app.get("/api/memory")
def get_memory(request: Request) -> dict[str, Any]:
    username = _require_session(request)
    return {"items": _load_memory(username)}


class MemoryPayload(BaseModel):
    items: list[str]


@app.put("/api/memory")
def put_memory(payload: MemoryPayload, request: Request) -> dict[str, bool]:
    username = _require_session(request)
    cleaned = [str(x).strip() for x in payload.items if str(x).strip()][:50]
    _save_memory(username, cleaned)
    return {"ok": True}


def _compose_effective_message(username: str, user_message: str, pdf_context: str) -> str:
    parts: list[str] = []
    mem = _memory_block(username)
    if mem:
        parts.append(mem)
    if pdf_context:
        parts.append(pdf_context)
    parts.append(user_message)
    return "\n\n".join(parts)


@app.post("/api/chat")
def chat(payload: ChatPayload, request: Request) -> StreamingResponse:
    """자동 모드: 명세 → 코드 전체 실행 (바로 진행)."""
    username = _require_session(request)
    images, pdf_context = _parse_attachments(payload.attachments)
    effective_message = _compose_effective_message(username, payload.message, pdf_context)

    def stream():
        try:
            for chunk in pipeline.pipe(
                user_message=effective_message,
                messages=payload.messages,
                username=username,
                chat_id=payload.chat_id,
                images=images or None,
            ):
                if chunk:
                    yield chunk
        except Exception as exc:  # noqa: BLE001
            yield f"\n\n[서버 오류] {exc}"

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")


@app.post("/api/chat/spec")
def chat_generate_spec(payload: ChatPayload, request: Request) -> dict[str, Any]:
    """'확인 후 진행' 모드 1단계.

    코딩 요청이면 명세를 생성해 반환 (`{"mode": "spec", "spec": {...}}`).
    일반 질문이면 채팅 응답을 바로 생성해 반환 (`{"mode": "chat", "reply": "..."}`).
    """
    username = _require_session(request)
    images, pdf_context = _parse_attachments(payload.attachments)
    effective_message = _compose_effective_message(username, payload.message, pdf_context)

    # 코딩 요청 여부 판정 — 아니면 명세 단계를 건너뛰고 바로 채팅 응답
    if not Pipeline._is_coding_request(payload.message):
        reply_chunks: list[str] = []
        try:
            for chunk in pipeline.generate_chat_reply(
                effective_message,
                messages=payload.messages,
                images=images or None,
            ):
                if chunk:
                    reply_chunks.append(chunk)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"mode": "chat", "reply": "".join(reply_chunks)}

    try:
        spec = pipeline.generate_spec(
            user_message=effective_message,
            messages=payload.messages,
            username=username,
            chat_id=payload.chat_id,
            images=images or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"mode": "spec", "spec": spec}


class CodeFromSpecPayload(BaseModel):
    message: str = ""
    chat_id: str | None = None
    spec: dict[str, Any]
    messages: list[dict[str, Any]] | None = None


@app.post("/api/chat/code")
def chat_generate_code(
    payload: CodeFromSpecPayload, request: Request
) -> StreamingResponse:
    """'확인 후 진행' 모드 2단계: (사용자가 검토/수정한) 명세로 코드 생성."""
    username = _require_session(request)

    # 사용자가 수정한 명세를 다시 저장 (최신본 갱신)
    try:
        pipeline.save_spec(
            username=username,
            chat_id=payload.chat_id,
            spec=payload.spec,
            user_message=payload.message,
        )
    except Exception:  # noqa: BLE001
        pass

    def stream():
        try:
            for chunk in pipeline.generate_code_from_spec(
                spec=payload.spec,
                user_message=payload.message,
                messages=payload.messages,
                username=username,
                chat_id=payload.chat_id,
            ):
                if chunk:
                    yield chunk
        except Exception as exc:  # noqa: BLE001
            yield f"\n\n[서버 오류] {exc}"

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# Spec management (per-user)
# ---------------------------------------------------------------------------
SPECS_DIR = ROOT / "specs"


@app.get("/api/specs")
def list_specs(request: Request) -> dict[str, Any]:
    """현재 로그인한 사용자의 저장된 명세서 목록을 반환합니다."""
    username = _require_session(request)
    user_dir = _spec_dir(username)  # sanitize_path_component 적용된 경로
    if not user_dir.exists():
        return {"specs": []}
    specs: list[dict[str, Any]] = []
    # spec_storage 는 YYYYMMDD.jsonl (JSONL, 1줄=1레코드) 형식으로 저장
    for path in sorted(user_dir.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            specs.append(
                {
                    "file": path.name,
                    "chat_id": record.get("chat_id"),
                    "saved_at": record.get("saved_at"),
                    "user_message": record.get("user_message"),
                    "project": (record.get("spec") or {}).get("project"),
                }
            )
    return {"specs": specs}


@app.get("/api/specs/{chat_id}")
def get_spec(chat_id: str, request: Request) -> dict[str, Any]:
    username = _require_session(request)
    spec = Pipeline.load_spec(username=username, chat_id=chat_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="명세서를 찾을 수 없습니다")
    return {"spec": spec}


_ensure_default_user()


# ---------------------------------------------------------------------------
# 미리보기 iframe 정적 자산 처리 (style.css, script.js 404 방지)
# ---------------------------------------------------------------------------
# 생성된 HTML이 단일 파일 내 인라인이 아닌 외부 파일(style.css, script.js 등)을
# 참조할 경우, 미리보기 iframe이 현재 서버 오리진에서 해당 파일을 요청합니다.
# 파일이 존재하지 않아 404가 발생하면 로그가 지저분해지므로, 정적 자산 확장자로
# 보이는 경로는 빈 컨텐츠(204)로 조용히 응답합니다.
_STATIC_PREVIEW_EXTS = (
    ".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf",
)


@app.get("/{asset_path:path}")
def preview_static_fallback(asset_path: str) -> Response:
    lowered = asset_path.lower()
    if lowered.endswith(_STATIC_PREVIEW_EXTS):
        # 미리보기용 빈 응답 — 로그에 404가 남지 않도록 204 반환
        if lowered.endswith(".css"):
            return Response(content="", media_type="text/css", status_code=204)
        if lowered.endswith((".js", ".mjs")):
            return Response(
                content="", media_type="application/javascript", status_code=204
            )
        return Response(status_code=204)
    raise HTTPException(status_code=404, detail="Not Found")


if __name__ == "__main__":
    print("Vibe Coding server starting...")
    print("Open in browser: http://127.0.0.1:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)