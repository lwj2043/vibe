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
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
USERS_PATH = ROOT / "users.json"
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
    salt = secrets.token_hex(16)
    _save_users(
        [
            {
                "username": "admin",
                "salt": salt,
                "password_hash": _hash_password("admin", salt),
            }
        ]
    )


def _verify_credentials(username: str, password: str) -> bool:
    for user in _load_users():
        if user.get("username") != username:
            continue
        salt = user.get("salt", "")
        expected = user.get("password_hash", "")
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
