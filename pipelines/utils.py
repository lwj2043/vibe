"""Parsing, validation, and formatting utilities."""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from datetime import datetime
from typing import Any

import httpx


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_json(text: str) -> dict[str, Any]:
    stripped = text.strip()

    # Harmony/channel 스타일 마커 제거
    stripped = re.sub(r"<\|?/?[a-zA-Z_][^>]*\|?>", "", stripped).strip()

    if stripped.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
        if match:
            stripped = match.group(1).strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
        start = stripped.find("{")
        while start != -1 and parsed is None:
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(stripped)):
                ch = stripped[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            candidate = stripped[start : i + 1]
                            try:
                                parsed = json.loads(candidate)
                            except json.JSONDecodeError:
                                parsed = None
                            break
            if parsed is None:
                start = stripped.find("{", start + 1)
        if parsed is None:
            raise ValueError(
                f"JSON 객체를 찾을 수 없습니다. 응답 미리보기: {stripped[:200]!r}"
            )

    if not isinstance(parsed, dict):
        raise ValueError("Top-level JSON must be an object")
    return parsed


def validate_spec(spec: dict[str, Any]) -> None:
    required = {"project", "files", "components", "api", "constraints", "user_story"}
    missing = sorted(required - set(spec))
    if missing:
        raise ValueError(f"Missing required keys: {', '.join(missing)}")
    if not isinstance(spec["files"], list) or not spec["files"]:
        raise ValueError("files must be a non-empty list")
    for item in spec["files"]:
        if not isinstance(item, dict) or not item.get("path"):
            raise ValueError("each file item must include a path")
    project = spec.get("project", {})
    if not isinstance(project, dict):
        raise ValueError("project must be an object")
    for key in ("name", "type", "scope", "tech_stack", "description"):
        if key not in project:
            raise ValueError(f"project.{key} is required")
    allowed_scopes = {"frontend", "backend", "fullstack", "cli", "data", "general"}
    if project.get("scope") not in allowed_scopes:
        raise ValueError(
            f"project.scope must be one of: {', '.join(sorted(allowed_scopes))}"
        )


def validate_diff_spec(diff: dict[str, Any]) -> None:
    allowed_keys = {"modified_files", "new_constraints", "removed_components"}
    if not any(key in diff for key in allowed_keys):
        raise ValueError(
            f"diff spec must contain at least one of: {', '.join(sorted(allowed_keys))}"
        )
    if "modified_files" in diff:
        if not isinstance(diff["modified_files"], list):
            raise ValueError("modified_files must be a list")
        for item in diff["modified_files"]:
            if not isinstance(item, dict) or not item.get("path"):
                raise ValueError("each modified_file must have a path")


_EXT_TO_LANG: dict[str, str] = {
    "html": "html", "htm": "html",
    "css": "css",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "py": "python",
    "json": "json",
    "md": "markdown",
    "sh": "bash", "bash": "bash",
    "yml": "yaml", "yaml": "yaml",
    "sql": "sql",
    "rs": "rust",
    "go": "go",
    "java": "java",
    "php": "php",
    "rb": "ruby",
    "swift": "swift",
    "kt": "kotlin",
    "xml": "xml",
    "svg": "xml",
    "toml": "toml",
}


def format_code_for_webui(text: str) -> str:
    """Convert ```filepath blocks to header + standard language hint."""

    def _replace(match: re.Match) -> str:  # type: ignore[type-arg]
        filepath = match.group(1).strip()
        code = match.group(2)
        ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
        lang = _EXT_TO_LANG.get(ext, ext)
        return f"\n**📄 `{filepath}`**\n```{lang}\n{code}```"

    return re.sub(
        r"```([^\n`\s]+\.[^\n`\s]+)\n(.*?)```",
        _replace,
        text,
        flags=re.DOTALL,
    )


def format_model_error(stage: str, exc: Exception, ollama_url: str) -> str:
    message = str(exc)
    lowered = message.lower()
    if (
        "all connection attempts failed" in lowered
        or "connection refused" in lowered
        or "name or service not known" in lowered
        or "nodename nor servname" in lowered
        or isinstance(exc, httpx.ConnectError)
    ):
        return (
            f"{stage} 중 Ollama 서버({ollama_url})에 연결하지 못했습니다.\n"
            "1) Ollama가 실행 중인지 확인하세요 (`ollama serve`).\n"
            "2) `pipelines/config.json`의 `ollama_url`이 올바른지 확인하세요.\n"
            "3) 네트워크/방화벽이 해당 포트(보통 11434)를 막고 있지 않은지 확인하세요."
        )
    if "404" in message and "model" in lowered:
        return (
            f"{stage} 중 모델을 찾을 수 없습니다: {message}\n"
            "`ollama list`로 설치된 모델 이름을 확인한 뒤 `pipelines/config.json`의 모델명을 수정하세요."
        )
    if "503" in message or "Service Unavailable" in message:
        return (
            f"{stage} 중 모델이 일시적으로 응답하지 않습니다. "
            "잠시 후 다시 시도하거나 `pipelines/config.json`에서 다른 모델로 바꿔 주세요."
        )
    if "429" in message or "Too Many Requests" in message or "rate_limit" in lowered:
        return (
            f"{stage} 중 API 사용량 제한에 걸렸습니다. "
            "잠시 후 다시 시도하거나 다른 모델을 사용해 주세요."
        )
    return f"{stage} 중 모델 호출에 실패했습니다: {message}"


def stream_text(text: str, chunk_size: int = 600) -> Generator[str, None, None]:
    for index in range(0, len(text), chunk_size):
        yield text[index : index + chunk_size]


def sanitize_path_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", value.strip())
    return cleaned.strip("._") or "default"
