"""OpenAI-compatible /v1/chat/completions client (sync + stream + multimodal)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import queue
import threading
from collections.abc import Generator
from typing import Any

import httpx

from .history import build_model_messages


def _client_kwargs(timeout: float) -> dict[str, Any]:
    # LAN-hosted model servers should be reached directly, even if proxy env vars are broken.
    return {"timeout": timeout, "trust_env": False}

logger = logging.getLogger(__name__)

# 재시도 설정 — 일시적 네트워크/서버 오류만 재시도한다.
_RETRYABLE_STATUS = {429, 502, 503, 504}
_MAX_RETRIES = 2  # 즉, 총 시도 = 1 + 2 = 3 회
_BACKOFF_BASE_SEC = 1.0


def _is_transient(exc: BaseException) -> bool:
    """일시적(재시도 가치 있는) 오류인지 판정."""
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return False


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    json_body: dict[str, Any],
    headers: dict[str, str],
) -> httpx.Response:
    """일시적 오류에 대해 지수 백오프로 재시도한다."""
    last_exc: BaseException | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = await client.post(url, json=json_body, headers=headers)
            response.raise_for_status()
            return response
        except (httpx.HTTPStatusError, httpx.HTTPError) as exc:
            last_exc = exc
            if attempt >= _MAX_RETRIES or not _is_transient(exc):
                raise
            delay = _BACKOFF_BASE_SEC * (2 ** attempt)
            logger.warning(
                "LLM 호출 일시 오류 — %.1fs 후 재시도 (%d/%d): %s",
                delay, attempt + 1, _MAX_RETRIES, exc,
            )
            await asyncio.sleep(delay)
    # 도달 불가 — 위에서 raise 됨
    assert last_exc is not None
    raise last_exc


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

    async with httpx.AsyncClient(**_client_kwargs(600)) as client:
        response = await _post_with_retry(
            client,
            f"{base_url.rstrip('/')}/chat/completions",
            json_body=body,
            headers=_headers(api_key),
        )
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

    async with httpx.AsyncClient(**_client_kwargs(600)) as client:
        response = await _post_with_retry(
            client,
            f"{base_url.rstrip('/')}/chat/completions",
            json_body=body,
            headers=_headers(api_key),
        )
        payload = response.json()

    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected LLM response: {payload}") from exc


def stream_llm_with_image_sync(
    base_url: str,
    model: str,
    system: str,
    user_text: str,
    image_png_bytes: bytes | list[bytes],
    messages: list[dict[str, Any]] | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
) -> Generator[str, None, None]:
    """Stream SSE chunks from a multimodal /v1/chat/completions call (text + image).

    VLM 미지원 서버에서는 400/422 가 나올 수 있음 — 호출부에서 예외를 받아
    텍스트 전용 경로로 폴백하세요.
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
        "stream": True,
        "messages": chat_messages,
    }
    if temperature is not None:
        body["temperature"] = temperature

    q: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=64)
    url = f"{base_url.rstrip('/')}/chat/completions"

    async def _producer() -> None:
        try:
            async with httpx.AsyncClient(**_client_kwargs(600)) as client:
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
            async with httpx.AsyncClient(**_client_kwargs(600)) as client:
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
