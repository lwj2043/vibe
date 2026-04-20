"""Ollama API client: sync call, stream call, async-from-sync bridge."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import Generator
from typing import Any

import httpx

from .history import build_model_messages


def run_async(coro: Any) -> Any:
    """Run a coroutine safely from a synchronous context.

    Open WebUI may call from within an already-running event loop.
    In that case ``asyncio.run()`` raises RuntimeError, so we fall back to
    executing the coroutine in a dedicated daemon thread with its own loop.
    """
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


async def call_ollama(
    ollama_url: str,
    model: str,
    system: str,
    user: str,
    messages: list[dict[str, Any]] | None = None,
    keep_alive: Any = None,
) -> str:
    """Non-streaming Ollama /api/chat call."""
    request_body: dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": build_model_messages(
            system=system,
            user=user,
            messages=messages,
        ),
    }
    if keep_alive is not None:
        request_body["keep_alive"] = keep_alive

    async with httpx.AsyncClient(timeout=600) as client:
        response = await client.post(
            f"{ollama_url.rstrip('/')}/api/chat",
            json=request_body,
        )
        response.raise_for_status()
        payload = response.json()

    try:
        return payload["message"]["content"]
    except KeyError as exc:
        raise RuntimeError(f"Unexpected Ollama response: {payload}") from exc


def stream_ollama_sync(
    ollama_url: str,
    model: str,
    system: str,
    user: str,
    messages: list[dict[str, Any]] | None = None,
    keep_alive: Any = None,
) -> Generator[str, None, None]:
    """Stream Ollama tokens via a thread + queue bridge."""
    request_body: dict[str, Any] = {
        "model": model,
        "stream": True,
        "messages": build_model_messages(
            system=system,
            user=user,
            messages=messages,
        ),
    }
    if keep_alive is not None:
        request_body["keep_alive"] = keep_alive

    q: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=64)
    url = f"{ollama_url.rstrip('/')}/api/chat"

    async def _producer() -> None:
        try:
            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream("POST", url, json=request_body) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        chunk = (data.get("message") or {}).get("content") or ""
                        if chunk:
                            q.put(("chunk", chunk))
                        if data.get("done"):
                            break
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
