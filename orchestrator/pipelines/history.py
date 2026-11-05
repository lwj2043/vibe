"""Chat history normalization."""

from __future__ import annotations

import re
from typing import Any

# 이전 턴의 주입물 제거 패턴
_REF_BLOCK_RE = re.compile(
    r"\n*---\s*참고 자료\s*---[\s\S]*?---\s*/참고 자료\s*---\n*",
    re.IGNORECASE,
)
_REF_TAIL_RE = re.compile(
    r"\n*위 '참고 자료'[\s\S]*?거절 문구[\s\S]*?금지\.?\s*$",
)
_FETCH_STATUS_RE = re.compile(r"^🌐 외부 데이터 수집 중\.{0,3}\s*", re.MULTILINE)

# 히스토리 길이 제한
HISTORY_MAX_TURNS = 8
HISTORY_PER_MSG_CHARS = 1500
HISTORY_TOTAL_CHARS = 6000


def normalize_chat_messages(
    messages: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    if not messages:
        return normalized

    for raw in messages[-HISTORY_MAX_TURNS:]:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = raw.get("content")
        if content is None:
            continue
        text = str(content)
        text = _REF_BLOCK_RE.sub("\n", text)
        text = _REF_TAIL_RE.sub("", text)
        text = _FETCH_STATUS_RE.sub("", text)
        text = text.strip()
        if not text:
            continue
        if len(text) > HISTORY_PER_MSG_CHARS:
            head = text[: HISTORY_PER_MSG_CHARS // 2]
            tail = text[-HISTORY_PER_MSG_CHARS // 2 :]
            text = f"{head}\n...(중략)...\n{tail}"
        normalized.append({"role": role, "content": text})

    total = 0
    kept_reversed: list[dict[str, str]] = []
    for msg in reversed(normalized):
        total += len(msg["content"])
        if total > HISTORY_TOTAL_CHARS and kept_reversed:
            break
        kept_reversed.append(msg)
    return list(reversed(kept_reversed))


def build_model_messages(
    system: str,
    user: str = "",
    messages: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    conversation = normalize_chat_messages(messages)
    user = (user or "").strip()
    if user:
        if (
            not conversation
            or conversation[-1]["role"] != "user"
            or conversation[-1]["content"] != user
        ):
            conversation.append({"role": "user", "content": user})
    return [{"role": "system", "content": system}, *conversation]
