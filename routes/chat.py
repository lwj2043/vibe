"""채팅 관련 라우트와 헬퍼.

소유 디렉터리: ``chat_logs/``
- state.json (프런트 복원용)
- log.jsonl (감사 로그, 5MB 초과 시 gzip 회전)
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from pipelines.dual_model_pipeline import Pipeline

from . import memory as memory_module
from ._pipeline import pipeline
from .auth import require_session

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CHAT_LOGS_DIR = ROOT / "chat_logs"

# 회전 임계치: log.jsonl 가 이 크기를 넘으면 gzip 으로 회전한다.
_LOG_ROTATE_BYTES = 5 * 1024 * 1024  # 5 MB
# 보존 개수: 회전된 gz 아카이브를 이 개수까지만 유지한다.
_LOG_KEEP_ARCHIVES = 5


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


def _rotate_log_if_needed(jsonl_path: Path) -> None:
    """log.jsonl 이 임계치를 넘으면 log.<ts>.jsonl.gz 로 회전한다."""
    try:
        if not jsonl_path.exists() or jsonl_path.stat().st_size < _LOG_ROTATE_BYTES:
            return
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = jsonl_path.with_name(f"log.{ts}.jsonl.gz")
        with jsonl_path.open("rb") as src, gzip.open(archive, "wb") as dst:
            shutil.copyfileobj(src, dst)
        jsonl_path.unlink()
        archives = sorted(
            jsonl_path.parent.glob("log.*.jsonl.gz"),
            key=lambda p: p.name,
            reverse=True,
        )
        for old in archives[_LOG_KEEP_ARCHIVES:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


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
    """모든 채팅의 (user → assistant) 턴을 {date,time,input,response} 로 변환."""
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


def _parse_attachments(
    attachments: list[dict[str, Any]] | None,
) -> tuple[list[bytes], str]:
    """클라이언트가 보낸 attachments → (이미지 바이트 목록, PDF 텍스트 컨텍스트)."""
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
        from io import BytesIO

        from pypdf import PdfReader
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


def _compose_effective_message(username: str, user_message: str, pdf_context: str) -> str:
    parts: list[str] = []
    mem = memory_module.memory_block(username)
    if mem:
        parts.append(mem)
    if pdf_context:
        parts.append(pdf_context)
    parts.append(user_message)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
router = APIRouter()


@router.get("/api/chats")
def get_chats(request: Request) -> dict:
    username = require_session(request)
    state = _state_path(username)
    if state.exists():
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            return {"chats": data.get("chats", []), "settings": data.get("settings")}
        except (json.JSONDecodeError, OSError):
            return {"chats": [], "settings": None}
    legacy = _legacy_single_file_path(username)
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            return {"chats": data.get("chats", []), "settings": data.get("settings")}
        except (json.JSONDecodeError, OSError):
            pass
    return {"chats": [], "settings": None}


class SaveChatsPayload(BaseModel):
    chats: list
    settings: dict | None = None


@router.put("/api/chats")
def save_chats(payload: SaveChatsPayload, request: Request) -> dict:
    username = require_session(request)
    CHAT_LOGS_DIR.mkdir(exist_ok=True)

    settings = _sanitize_settings(payload.settings)
    raw_chats = [chat for chat in payload.chats if isinstance(chat, dict)]
    chats = [_sanitize_chat(chat) for chat in raw_chats]

    state_path = _state_path(username)
    state_path.write_text(
        json.dumps({"chats": chats, "settings": settings}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

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
        _rotate_log_if_needed(jsonl_path)

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


@router.post("/api/chat")
def chat(payload: ChatPayload, request: Request) -> StreamingResponse:
    """자동 모드: 명세 → 코드 전체 실행 (바로 진행)."""
    username = require_session(request)
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
            logger.exception("chat stream 실패")
            yield f"\n\n[서버 오류] {exc}"

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")


@router.post("/api/chat/spec")
def chat_generate_spec(payload: ChatPayload, request: Request) -> dict[str, Any]:
    """'확인 후 진행' 모드 1단계: 코딩 요청이면 명세, 아니면 채팅 응답."""
    username = require_session(request)
    images, pdf_context = _parse_attachments(payload.attachments)
    effective_message = _compose_effective_message(username, payload.message, pdf_context)

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


@router.post("/api/chat/code")
def chat_generate_code(
    payload: CodeFromSpecPayload, request: Request
) -> StreamingResponse:
    """'확인 후 진행' 모드 2단계: (사용자가 검토/수정한) 명세로 코드 생성."""
    username = require_session(request)

    try:
        pipeline.save_spec(
            username=username,
            chat_id=payload.chat_id,
            spec=payload.spec,
            user_message=payload.message,
        )
    except Exception:  # noqa: BLE001
        logger.warning("save_spec 실패 (코드 생성은 계속 진행)", exc_info=True)

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
            logger.exception("chat stream 실패")
            yield f"\n\n[서버 오류] {exc}"

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")
