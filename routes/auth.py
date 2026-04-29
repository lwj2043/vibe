"""Authentication, session, and rate-limit module.

소유 파일: ``users.json``.

다른 라우트 모듈은 ``require_session`` / ``SESSIONS`` 만 의존성으로 import 한다.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections import deque
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
USERS_PATH = ROOT / "users.json"

# 현재 권장 반복 수. 신규 해시 생성 시 사용.
# 기존(200k) 해시를 가진 사용자는 다음 로그인 성공 시 자동으로 이 값으로 재해시된다.
PBKDF2_ITERS = 600_000
# 인증 시 허용되는 과거 반복 수(점진 마이그레이션).
PBKDF2_LEGACY_ITERS = (200_000,)

# token -> username
SESSIONS: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Rate limiting (per-IP, in-process)
# ---------------------------------------------------------------------------
_LOGIN_RATE_WINDOW = 60.0
_LOGIN_RATE_LIMIT = 5
_REGISTER_RATE_LIMIT = 5
_login_attempts: dict[str, deque[float]] = {}
_register_attempts: dict[str, deque[float]] = {}
_rate_lock = threading.Lock()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _check_rate(bucket: dict[str, deque[float]], ip: str, limit: int) -> None:
    now = monotonic()
    cutoff = now - _LOGIN_RATE_WINDOW
    with _rate_lock:
        dq = bucket.setdefault(ip, deque())
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            retry_after = max(1, int(_LOGIN_RATE_WINDOW - (now - dq[0])))
            raise HTTPException(
                status_code=429,
                detail=f"요청이 너무 많습니다. {retry_after}초 후 다시 시도해주세요.",
                headers={"Retry-After": str(retry_after)},
            )


def _record_attempt(bucket: dict[str, deque[float]], ip: str) -> None:
    now = monotonic()
    with _rate_lock:
        bucket.setdefault(ip, deque()).append(now)


# ---------------------------------------------------------------------------
# users.json helpers
# ---------------------------------------------------------------------------
def _hash_password(password: str, salt: str, iters: int = PBKDF2_ITERS) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), iters
    )
    return dk.hex()


def _make_password_hash(password: str) -> str:
    """현재 권장 반복 수로 해시 생성. 형식: ``iters$salt$hash``."""
    salt = secrets.token_hex(16)
    return f"{PBKDF2_ITERS}${salt}${_hash_password(password, salt, PBKDF2_ITERS)}"


def _parse_password_hash(stored: str) -> tuple[int, str, str] | None:
    """저장된 해시를 (iters, salt, hash) 로 분해.

    지원 형식:
      - ``iters$salt$hash``  (현재)
      - ``salt:hash``        (레거시 — 200_000 반복으로 가정)
    """
    if not stored:
        return None
    if "$" in stored:
        parts = stored.split("$")
        if len(parts) != 3:
            return None
        try:
            iters = int(parts[0])
        except ValueError:
            return None
        salt, expected = parts[1], parts[2]
        if not salt or not expected:
            return None
        return iters, salt, expected
    if ":" in stored:
        salt, expected = stored.split(":", 1)
        if not salt or not expected:
            return None
        return PBKDF2_LEGACY_ITERS[0], salt, expected
    return None


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


def ensure_default_user() -> None:
    """users.json 이 없으면 admin/admin 계정을 생성한다."""
    if USERS_PATH.exists():
        return
    _save_users([{"username": "admin", "password_hash": _make_password_hash("admin")}])


def _verify_credentials(username: str, password: str) -> bool:
    """로그인 검증. 해시가 옛 반복 수면 통과 시 자동으로 재해시한다."""
    users = _load_users()
    for idx, user in enumerate(users):
        if user.get("username") != username:
            continue
        parsed = _parse_password_hash(user.get("password_hash", ""))
        # 레거시 별도 salt 필드 폴백
        if parsed is None and user.get("salt") and user.get("password_hash"):
            parsed = (PBKDF2_LEGACY_ITERS[0], user["salt"], user["password_hash"])
        if parsed is None:
            return False
        iters, salt, expected = parsed
        candidate = _hash_password(password, salt, iters)
        if not secrets.compare_digest(candidate, expected):
            return False
        # 인증 성공 — 반복 수가 낮거나 형식이 옛 것이면 재해시(자동 마이그레이션)
        if iters < PBKDF2_ITERS:
            try:
                users[idx] = {
                    "username": username,
                    "password_hash": _make_password_hash(password),
                }
                _save_users(users)
            except OSError:
                pass  # 재해시 실패해도 로그인 자체는 성공으로 처리
        return True
    return False


def require_session(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if not token or token not in SESSIONS:
        raise HTTPException(status_code=401, detail="인증이 필요합니다")
    return SESSIONS[token]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
router = APIRouter()


class LoginPayload(BaseModel):
    username: str = Field(..., max_length=128)
    password: str = Field(..., max_length=512)


class RegisterPayload(BaseModel):
    username: str = Field(..., max_length=128)
    password: str = Field(..., max_length=512)


@router.post("/api/login")
def login(payload: LoginPayload, request: Request) -> dict[str, str]:
    ip = client_ip(request)
    _check_rate(_login_attempts, ip, _LOGIN_RATE_LIMIT)
    if not _verify_credentials(payload.username, payload.password):
        _record_attempt(_login_attempts, ip)
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = payload.username
    return {"token": token, "username": payload.username}


@router.post("/api/logout")
def logout(request: Request) -> dict[str, bool]:
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    SESSIONS.pop(token, None)
    return {"ok": True}


@router.post("/api/register")
def register(payload: RegisterPayload, request: Request) -> dict[str, str]:
    ip = client_ip(request)
    _check_rate(_register_attempts, ip, _REGISTER_RATE_LIMIT)
    _record_attempt(_register_attempts, ip)
    username = payload.username.strip()
    if not username or not payload.password:
        raise HTTPException(status_code=400, detail="아이디와 비밀번호를 입력해주세요")
    if len(username) < 2:
        raise HTTPException(status_code=400, detail="아이디는 2자 이상이어야 합니다")
    if len(username) > 64:
        raise HTTPException(status_code=400, detail="아이디는 64자 이하여야 합니다")
    if len(payload.password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다")
    if len(payload.password) > 256:
        raise HTTPException(status_code=400, detail="비밀번호는 256자 이하여야 합니다")
    users = _load_users()
    if any(u.get("username") == username for u in users):
        raise HTTPException(status_code=409, detail="이미 존재하는 아이디입니다")
    users.append({"username": username, "password_hash": _make_password_hash(payload.password)})
    _save_users(users)
    return {"ok": "registered"}
