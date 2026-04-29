"""Spec 관리 라우트.

- GET /api/specs            : 현재 사용자의 저장된 명세서 목록
- GET /api/specs/{chat_id}  : 특정 채팅의 최신 명세서
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from pipelines.dual_model_pipeline import Pipeline
from pipelines.spec_storage import spec_dir as _spec_dir

from .auth import require_session

router = APIRouter()


@router.get("/api/specs")
def list_specs(request: Request) -> dict[str, Any]:
    """현재 로그인한 사용자의 저장된 명세서 목록을 반환."""
    username = require_session(request)
    user_dir = _spec_dir(username)
    if not user_dir.exists():
        return {"specs": []}
    specs: list[dict[str, Any]] = []
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


@router.get("/api/specs/{chat_id}")
def get_spec(chat_id: str, request: Request) -> dict[str, Any]:
    username = require_session(request)
    spec = Pipeline.load_spec(username=username, chat_id=chat_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="명세서를 찾을 수 없습니다")
    return {"spec": spec}
