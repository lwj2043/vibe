"""dual_model_pipeline 의 generator 출력 → OpenAI 호환 형식 변환 어댑터.

- ``stream_to_openai_sse``: stream=true 응답용. ``data: {json}\\n\\n`` 청크들을
  순차적으로 yield 한 뒤 마지막에 ``data: [DONE]``.
- ``collect_to_chat_completion``: stream=false 응답용. 모든 청크를 모아 단일
  ``chat.completion`` 객체로 반환.

dual_model_pipeline.pipe() 의 generator 는 다음 종류의 청크를 흘린다:
- 일반 텍스트 (LLM 출력)
- ``\\x01STATUS:...\\x02`` 형태의 상태 마커 (UI 진행 표시용; OpenAI 클라이언트는
  이해 못 하므로 본문에서 제거)
- 빈 문자열 ("" 또는 " " heartbeat)

상태 마커는 OpenAI 응답 본문에서 제거한다. UI 단계 정보가 필요하면 향후
``delta.metadata.status`` 필드로 분리해 흘려보낼 수 있다.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Generator, Iterable

_STATUS_BEGIN = "\x01"
_STATUS_END = "\x02"


def _strip_status_markers(text: str) -> str:
    """``\\x01STATUS:...\\x02`` 패턴을 제거."""
    if _STATUS_BEGIN not in text:
        return text

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == _STATUS_BEGIN:
            end = text.find(_STATUS_END, i + 1)
            if end == -1:
                # 닫히지 않은 마커 — 안전하게 끝까지 버린다.
                break
            i = end + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _make_chunk_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _sse_data(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def stream_to_openai_sse(
    chunks: Iterable[str],
    model: str,
) -> Generator[bytes, None, None]:
    """generator → OpenAI SSE 바이트 스트림.

    StreamingResponse 의 generator 로 그대로 넘기면 된다.
    """
    chunk_id = _make_chunk_id()
    created = int(time.time())

    # 첫 청크: role="assistant" 시그널
    yield _sse_data(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }
            ],
        }
    )

    try:
        for raw in chunks:
            if not raw:
                continue
            text = _strip_status_markers(raw)
            if not text:
                continue
            yield _sse_data(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": text},
                            "finish_reason": None,
                        }
                    ],
                }
            )
    except Exception as exc:  # noqa: BLE001
        # 스트림 중간에 예외가 나도 클라이언트가 영원히 매달려 있지 않도록
        # 짧은 안내 문구를 본문에 흘리고 정상 종료한다.
        err_text = f"\n\n[서버 오류] {exc}"
        yield _sse_data(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": err_text},
                        "finish_reason": None,
                    }
                ],
            }
        )

    # 종료 청크
    yield _sse_data(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        }
    )
    yield b"data: [DONE]\n\n"


def collect_to_chat_completion(
    chunks: Iterable[str],
    model: str,
) -> dict:
    """비스트리밍: 모든 청크를 모아 chat.completion 1회 객체로."""
    chunk_id = _make_chunk_id()
    created = int(time.time())

    parts: list[str] = []
    for raw in chunks:
        if not raw:
            continue
        parts.append(_strip_status_markers(raw))

    content = "".join(parts).strip()

    return {
        "id": chunk_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        # token 카운트는 정확히 모름 — 0 으로 채워 OpenAI 클라이언트 호환만 유지.
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
