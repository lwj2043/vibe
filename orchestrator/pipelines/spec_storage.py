"""Per-user spec persistence (JSONL day files + latest.json)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import SPECS_ROOT
from .utils import now_iso, sanitize_path_component

logger = logging.getLogger(__name__)

# 보존 기간(일). 이보다 오래된 spec dayfile 은 save 시 정리한다.
SPEC_RETENTION_DAYS = 90

# specs/{username}/YYYYMMDD.jsonl
_SPEC_DAYFILE_RE = re.compile(r"^\d{8}\.jsonl$")

# spec 내부 키 정렬 순서
_SPEC_KEY_ORDER = (
    "project", "files", "components", "data_model",
    "api", "logic", "styling", "constraints", "notes",
)
# 레코드 최상위 키 순서
_RECORD_KEY_ORDER = (
    "saved_at", "chat_id", "user_message", "spec",
)


def spec_dir(username: str) -> Path:
    safe = sanitize_path_component(username or "anonymous")
    path = SPECS_ROOT / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def spec_dayfile(username: str, when: datetime | None = None) -> Path:
    d = spec_dir(username)
    return d / ((when or datetime.now()).strftime("%Y%m%d") + ".jsonl")


def _ordered_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        return spec
    ordered: dict[str, Any] = {}
    for k in _SPEC_KEY_ORDER:
        if k in spec:
            ordered[k] = spec[k]
    for k, v in spec.items():
        if k not in ordered:
            ordered[k] = v
    return ordered


def _ordered_record(record: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for k in _RECORD_KEY_ORDER:
        if k in record:
            ordered[k] = record[k]
    for k, v in record.items():
        if k not in ordered:
            ordered[k] = v
    if isinstance(ordered.get("spec"), dict):
        ordered["spec"] = _ordered_spec(ordered["spec"])
    return ordered


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except OSError:
        return []
    return out


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    lines = [
        json.dumps(r, ensure_ascii=False, separators=(",", ":"))
        for r in records
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _find_chat_in_dayfiles(
    username: str, chat_id: str | None
) -> tuple[Path, int] | None:
    """Find the most-recent record matching chat_id across dayfiles."""
    if not chat_id:
        return None
    d = spec_dir(username)
    for f in sorted(d.glob("*.jsonl"), reverse=True):
        if not _SPEC_DAYFILE_RE.match(f.name):
            continue
        recs = _read_jsonl(f)
        for i in range(len(recs) - 1, -1, -1):
            if recs[i].get("chat_id") == chat_id:
                return f, i
    return None


def _prune_old_dayfiles(d: Path, retention_days: int = SPEC_RETENTION_DAYS) -> None:
    """retention_days 보다 오래된 YYYYMMDD.jsonl 파일을 삭제한다.

    파일명의 날짜를 기준으로 판정하므로 mtime 변화에 영향을 받지 않는다.
    """
    if retention_days <= 0:
        return
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y%m%d")
    for f in d.glob("*.jsonl"):
        if not _SPEC_DAYFILE_RE.match(f.name):
            continue
        if f.stem < cutoff:
            try:
                f.unlink()
            except OSError as exc:
                logger.warning("spec dayfile 삭제 실패: %s (%s)", f, exc)


def _spec_signature(record: dict[str, Any]) -> tuple[str, str, str]:
    """중복 판정용 키 — saved_at 은 제외하고 내용만 본다."""
    return (
        str(record.get("chat_id") or ""),
        str(record.get("user_message") or ""),
        json.dumps(record.get("spec") or {}, sort_keys=True, ensure_ascii=False),
    )


def save_spec(
    username: str,
    chat_id: str | None,
    spec: dict[str, Any],
    user_message: str = "",
) -> Path:
    d = spec_dir(username)
    record = _ordered_record({
        "saved_at": now_iso(),
        "chat_id": chat_id,
        "user_message": user_message,
        "spec": spec,
    })

    path = spec_dayfile(username)
    recs = _read_jsonl(path)

    # 직전 레코드와 내용이 같으면 append 하지 않는다(saved_at 제외 비교).
    new_sig = _spec_signature(record)
    if recs and _spec_signature(recs[-1]) == new_sig:
        # latest.json 만 갱신(타임스탬프 표시는 최신화)
        (d / "latest.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _prune_old_dayfiles(d)
        return path

    recs.append(record)
    _write_jsonl(path, recs)

    latest = d / "latest.json"
    latest.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 보존 기간 초과 파일 정리(저장과 함께 lazily 수행)
    _prune_old_dayfiles(d)
    return path


def load_spec(
    username: str, chat_id: str | None = None
) -> dict[str, Any] | None:
    found = _find_chat_in_dayfiles(username, chat_id)
    if found is not None:
        path, idx = found
        recs = _read_jsonl(path)
        if 0 <= idx < len(recs):
            spec = recs[idx].get("spec")
            if isinstance(spec, dict):
                return spec
    latest = spec_dir(username) / "latest.json"
    if latest.exists():
        try:
            rec = json.loads(latest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if isinstance(rec, dict) and isinstance(rec.get("spec"), dict):
            return rec["spec"]
    return None
