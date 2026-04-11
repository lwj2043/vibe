"""FastAPI backend for Vibe Coding chat UI.

- Serves chat_ui.html
- /api/login: validates credentials against users.json (PBKDF2-hashed)
- /api/chat: streams pipeline output back to the browser

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
USERS_PATH = ROOT / "users.json"
CHAT_LOGS_DIR = ROOT / "chat_logs"
CHAT_UI_PATH = ROOT / "chat_ui.html"

# Allow `from pipelines.dual_model_pipeline import Pipeline`
sys.path.insert(0, str(ROOT))
from pipelines.dual_model_pipeline import Pipeline  # noqa: E402

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
    return FileResponse(CHAT_UI_PATH)


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
def _chat_log_path(username: str) -> Path:
    return CHAT_LOGS_DIR / f"{username}.json"


def _format_timestamp(timestamp_ms: Any) -> str | None:
    if not isinstance(timestamp_ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().isoformat(
            timespec="seconds"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _format_elapsed(duration_ms: int | None) -> str | None:
    if duration_ms is None or duration_ms < 0:
        return None
    if duration_ms < 1000:
        return f"{duration_ms}ms"

    seconds = duration_ms / 1000
    if seconds < 60:
        precision = 1 if seconds < 10 else 0
        return f"{seconds:.{precision}f}초"

    minutes = int(seconds // 60)
    remaining_seconds = seconds - (minutes * 60)
    if remaining_seconds < 1:
        return f"{minutes}분"

    precision = 1 if remaining_seconds < 10 else 0
    return f"{minutes}분 {remaining_seconds:.{precision}f}초"


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    return content if isinstance(content, str) else str(content)


def _attachment_names(message: dict[str, Any]) -> list[str]:
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return []

    names: list[str] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        name = attachment.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _build_turn_logs(chat: dict[str, Any]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    pending_user: dict[str, Any] | None = None

    for message in chat.get("messages", []):
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        if role == "user":
            pending_user = message
            continue
        if role != "assistant" or pending_user is None:
            continue

        user_timestamp = pending_user.get("timestamp")
        assistant_timestamp = message.get("timestamp")
        response_time_ms: int | None = None
        if isinstance(message.get("durationMs"), (int, float)):
            duration = int(message["durationMs"])
            if duration >= 0:
                response_time_ms = duration
        elif isinstance(user_timestamp, (int, float)) and isinstance(
            assistant_timestamp, (int, float)
        ):
            duration = int(assistant_timestamp - user_timestamp)
            if duration >= 0:
                response_time_ms = duration

        attachment_names = _attachment_names(pending_user)
        turns.append(
            {
                "turn_index": len(turns) + 1,
                "request_id": message.get("requestId") or pending_user.get("requestId"),
                "input_text": _message_text(pending_user),
                "output_text": _message_text(message),
                "model": chat.get("model"),
                "elapsed": _format_elapsed(response_time_ms),
                "attachment_count": len(attachment_names),
                "attachment_names": attachment_names,
            }
        )
        pending_user = None

    return turns


def _build_chat_summary(chat: dict[str, Any]) -> dict[str, Any]:
    turns = _build_turn_logs(chat)
    messages = chat.get("messages", [])
    return {
        "chat_id": chat.get("id"),
        "title": chat.get("title"),
        "model": chat.get("model"),
        "pinned": bool(chat.get("pinned")),
        "created_at": _format_timestamp(chat.get("createdAt")),
        "updated_at": _format_timestamp(chat.get("updatedAt")),
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "turn_count": len(turns),
        "turns": turns,
    }


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
    path = _chat_log_path(username)
    if not path.exists():
        return {"chats": [], "settings": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"chats": [], "settings": None}


class SaveChatsPayload(BaseModel):
    chats: list
    settings: dict | None = None


@app.put("/api/chats")
def save_chats(payload: SaveChatsPayload, request: Request) -> dict:
    username = _require_session(request)
    CHAT_LOGS_DIR.mkdir(exist_ok=True)
    path = _chat_log_path(username)
    settings = _sanitize_settings(payload.settings)
    chats = [_sanitize_chat(chat) for chat in payload.chats if isinstance(chat, dict)]
    chat_summaries = [
        _build_chat_summary(chat) for chat in payload.chats if isinstance(chat, dict)
    ]
    path.write_text(
        json.dumps(
            {
                "log_version": 3,
                "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "username": username,
                "chats": chats,
                "settings": settings,
                "conversation_logs": chat_summaries,
                "chat_summaries": chat_summaries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"ok": True}


class ChatPayload(BaseModel):
    message: str


@app.post("/api/chat")
def chat(payload: ChatPayload, request: Request) -> StreamingResponse:
    _require_session(request)

    def stream():
        try:
            for chunk in pipeline.pipe(user_message=payload.message):
                if chunk:
                    yield chunk
        except Exception as exc:  # noqa: BLE001
            yield f"\n\n[서버 오류] {exc}"

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")


_ensure_default_user()


if __name__ == "__main__":
    print("Vibe Coding server starting...")
    print("Open in browser: http://127.0.0.1:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
