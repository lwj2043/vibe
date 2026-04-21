"""OpenAI-compatible /v1/chat/completions client (sync + stream + multimodal)."""

from __future__ import annotations

import asyncio
import base64
import json
import queue
import threading
from collections.abc import Generator
from typing import Any

import httpx

from .history import build_model_messages


def run_async(coro: Any) -> Any:
    """Run a coroutine safely from a synchronous context, even if a loop
    is already running (falls back to a dedicated daemon thread)."""
    try:
        asyncio.get_running_loop()
        result_holder: list[Any] = []
        exc_holder: list[BaseException] = []

        def _run() -> None:
            try:
                result_holder.append(asyncio.run(coro))
            except BaseException as exc:  # noqa: BLE001
                exc_holder.append(exc)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join()
        if exc_holder:
            raise exc_holder[0]
        return result_holder[0]
    except RuntimeError:
        return asyncio.run(coro)


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def call_llm(
    base_url: str,
    model: str,
    system: str,
    user: str,
    messages: list[dict[str, Any]] | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
) -> str:
    """Non-streaming POST /v1/chat/completions."""
    body: dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": build_model_messages(system=system, user=user, messages=messages),
    }
    if temperature is not None:
        body["temperature"] = temperature

    async with httpx.AsyncClient(timeout=600) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=body,
            headers=_headers(api_key),
        )
        response.raise_for_status()
        payload = response.json()

    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected LLM response: {payload}") from exc


async def call_llm_with_image(
    base_url: str,
    model: str,
    system: str,
    user_text: str,
    image_png_bytes: bytes | list[bytes],
    api_key: str | None = None,
    temperature: float | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> str:
    """Multimodal POST /v1/chat/completions with one or more PNG/JPEG images.

    ``image_png_bytes`` 는 ``bytes`` 또는 ``list[bytes]``. 여러 이미지가
    주어지면 모두 OpenAI 표준 content-array 포맷의 image_url 항목으로 이어
    붙여 하나의 user 메시지로 전송합니다.

    VLM (vision-enabled) 모델에서만 동작합니다. 서버가 지원하지 않으면
    400/422 가 반환될 수 있습니다 — 호출부에서 예외로 받아서 텍스트 전용
    폴백을 수행해야 합니다.
    """
    if isinstance(image_png_bytes, (bytes, bytearray)):
        image_list = [bytes(image_png_bytes)]
    else:
        image_list = [bytes(img) for img in image_png_bytes if img]

    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for img in image_list:
        b64 = base64.b64encode(img).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    chat_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    if messages:
        chat_messages.extend(
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
        )
    chat_messages.append({"role": "user", "content": content})

    body: dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": chat_messages,
    }
    if temperature is not None:
        body["temperature"] = temperature

    async with httpx.AsyncClient(timeout=600) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=body,
            headers=_headers(api_key),
        )
        response.raise_for_status()
        payload = response.json()

    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected LLM response: {payload}") from exc


def stream_llm_sync(
    base_url: str,
    model: str,
    system: str,
    user: str,
    messages: list[dict[str, Any]] | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
) -> Generator[str, None, None]:
    """Stream SSE chunks from /v1/chat/completions via a thread + queue bridge."""
    body: dict[str, Any] = {
        "model": model,
        "stream": True,
        "messages": build_model_messages(system=system, user=user, messages=messages),
    }
    if temperature is not None:
        body["temperature"] = temperature

    q: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=64)
    url = f"{base_url.rstrip('/')}/chat/completions"

    async def _producer() -> None:
        try:
            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream(
                    "POST", url, json=body, headers=_headers(api_key)
                ) as resp:
                    resp.raise_for_status()
                    async for raw_line in resp.aiter_lines():
                        if not raw_line:
                            continue
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        try:
                            delta = data["choices"][0].get("delta") or {}
                        except (KeyError, IndexError, TypeError):
                            continue
                        chunk = delta.get("content") or ""
                        if chunk:
                            q.put(("chunk", chunk))
        except Exception as exc:  # noqa: BLE001
            q.put(("error", exc))
        finally:
            q.put(("done", None))

    def _run_loop() -> None:
        try:
            asyncio.run(_producer())
        except Exception as exc:  # noqa: BLE001
            q.put(("error", exc))
            q.put(("done", None))

    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()
    while True:
        kind, value = q.get()
        if kind == "chunk":
            yield value
        elif kind == "error":
            raise value  # type: ignore[misc]
        else:
            break
