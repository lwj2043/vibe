"""도구 레지스트리: ``tools/registry.json`` 로더 + 활성/접근 검사.

Brainrot tools/registry.json 의 단일 소스 패턴 차용. Vibe 는 인라인 + 사이드카 혼합이라
``kind`` 필드로 분기.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "tools" / "registry.json"
SIDECAR_HEALTH_TIMEOUT = float(os.getenv("VIBE_SIDECAR_HEALTH_TIMEOUT", "0.5"))
SIDECAR_HEALTH_CACHE_TTL = float(os.getenv("VIBE_SIDECAR_HEALTH_CACHE_TTL", "30"))
_SIDECAR_HEALTH_CACHE: dict[str, tuple[float, bool]] = {}


def _read_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        logger.warning("registry not found: %s", REGISTRY_PATH)
        return {"tools": []}
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("registry load failed: %s", exc)
        return {"tools": []}


def load_registry() -> dict[str, Any]:
    """파일 mtime 기반 캐시는 두지 않는다 — 호출 빈도 낮음."""
    return _read_registry()


def get_tool(tool_name: str) -> dict[str, Any] | None:
    for tool in load_registry().get("tools", []):
        if tool.get("name") == tool_name:
            return tool
    return None


def is_allowed(tool_name: str, user_email: str | None = None) -> bool:
    tool = get_tool(tool_name)
    if not tool:
        return False
    access = tool.get("access", "public")
    if access == "public":
        return True
    if access == "allowlist":
        env_name = tool.get("allowlist_emails_env", "")
        if not env_name:
            return False
        allowed = (os.getenv(env_name) or "").lower()
        if not user_email:
            return False
        return user_email.lower() in {e.strip() for e in allowed.split(",") if e.strip()}
    return False


def _is_inline_available(tool: dict[str, Any]) -> bool:
    module = tool.get("module")
    if not module:
        return False
    try:
        importlib.import_module(module)
        return True
    except ImportError as exc:
        logger.warning("inline tool %s unavailable: %s", tool.get("name"), exc)
        return False


def _is_sidecar_available(tool: dict[str, Any]) -> bool:
    url = sidecar_url(tool)
    if not url:
        return False
    cache_key = url.rstrip("/")
    now = time.monotonic()
    cached = _SIDECAR_HEALTH_CACHE.get(cache_key)
    if cached and now - cached[0] < SIDECAR_HEALTH_CACHE_TTL:
        return cached[1]
    try:
        with httpx.Client(timeout=SIDECAR_HEALTH_TIMEOUT) as client:
            resp = client.get(f"{cache_key}/health")
            available = resp.status_code == 200
    except httpx.HTTPError:
        available = False
    _SIDECAR_HEALTH_CACHE[cache_key] = (now, available)
    return available


def sidecar_url(tool: dict[str, Any]) -> str:
    env_name = tool.get("url_env", "")
    default = tool.get("default_url", "")
    if env_name:
        val = os.getenv(env_name, "").strip()
        if val:
            return val
    return default


def list_enabled_tools(check_sidecars: bool = True) -> list[dict[str, Any]]:
    """레지스트리의 모든 도구를 [{name, kind, display_name, description, available}] 로 반환.

    ``check_sidecars=True`` 면 사이드카 health 체크 수행 (slow). UI 첫 로드 외엔 False 권장.
    """
    out: list[dict[str, Any]] = []
    for tool in load_registry().get("tools", []):
        kind = tool.get("kind", "inline")
        if kind == "inline":
            available = _is_inline_available(tool)
        elif kind == "sidecar":
            available = _is_sidecar_available(tool) if check_sidecars else True
        else:
            available = False
        out.append({
            "name": tool.get("name", ""),
            "kind": kind,
            "display_name": tool.get("display_name", tool.get("name", "")),
            "description": tool.get("description", ""),
            "access": tool.get("access", "public"),
            "available": available,
        })
    return out


@lru_cache(maxsize=1)
def build_system_prompt_addendum() -> str:
    """``system_prompt_line`` 들을 합쳐 시스템 프롬프트에 부착할 텍스트 반환.

    Pipeline 이 매 요청마다 호출하지 않도록 캐시. 레지스트리 수정 시 서버 재시작 필요.
    """
    lines: list[str] = []
    for tool in load_registry().get("tools", []):
        line = (tool.get("system_prompt_line") or "").strip()
        if line:
            lines.append(line)
    if not lines:
        return ""
    return "\n[사용 가능한 도구]\n" + "\n".join(lines)
