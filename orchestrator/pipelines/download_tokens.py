"""TTL 기반 capability URL 토큰.

Brainrot pdf-service 의 uuid 기반 capability URL 패턴을 차용:
- 토큰 = ``secrets.token_urlsafe(24)`` — 추측 불가능한 랜덤 ID.
- 파일은 ``exports/<token>/<filename>`` 에 저장.
- 검증 시 ``mtime`` 비교 → TTL 경과 시 410, 파일 자동 삭제.
- 매 요청마다 ``sweep_expired()`` lazy 호출 (백그라운드 스레드 X).
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent  # orchestrator/
DATA_DIR = ROOT.parent / "data"                 # vibe-rebuild/data/
EXPORTS_DIR = DATA_DIR / "exports"
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TTL_SECONDS = int(os.getenv("VIBE_DOWNLOAD_TOKEN_TTL", "3600"))
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._\- 가-힣]+$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def sanitize_filename(name: str | None, default_stem: str = "document") -> str:
    """파일명을 안전한 문자만 허용하도록 정리. 확장자는 호출자가 보장."""
    name = (name or "").strip() or default_stem
    if not SAFE_NAME_RE.match(name):
        name = re.sub(r"[^A-Za-z0-9._\- 가-힣]", "_", name)
    if len(name) > 100:
        # 확장자 보존
        if "." in name[-10:]:
            stem, _, ext = name.rpartition(".")
            name = stem[: 100 - len(ext) - 1] + "." + ext
        else:
            name = name[:100]
    return name or default_stem


def issue_token(file_path: Path, ttl: int = DEFAULT_TTL_SECONDS) -> tuple[str, int]:
    """``file_path`` 내용을 ``exports/<token>/`` 로 옮기고 (token, expires_at_unix) 반환.

    이미 ``exports/`` 안에 있는 파일이면 mtime 만 갱신하고 새 토큰 발급.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"파일이 존재하지 않습니다: {file_path}")
    sweep_expired(ttl)

    token = secrets.token_urlsafe(24)
    target_dir = EXPORTS_DIR / token
    target_dir.mkdir(parents=True, exist_ok=False)
    target_path = target_dir / file_path.name
    shutil.move(str(file_path), str(target_path))
    try:
        target_path.chmod(0o600)
    except OSError:
        pass
    expires_at = int(time.time()) + ttl
    return token, expires_at


def resolve_token(token: str, filename: str) -> Path | None:
    """토큰 + 파일명 → 실제 경로. TTL 만료/없음 시 None."""
    if not TOKEN_RE.match(token):
        return None
    safe_name = sanitize_filename(filename)
    path = EXPORTS_DIR / token / safe_name
    if not path.exists() or not path.is_file():
        return None
    age = time.time() - path.stat().st_mtime
    if age > DEFAULT_TTL_SECONDS:
        try:
            shutil.rmtree(path.parent, ignore_errors=True)
        except OSError:
            pass
        return None
    return path


def sweep_expired(ttl: int = DEFAULT_TTL_SECONDS) -> int:
    """만료된 ``exports/<token>/`` 디렉터리를 삭제. 삭제 개수 반환."""
    now = time.time()
    deleted = 0
    if not EXPORTS_DIR.exists():
        return 0
    for entry in EXPORTS_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            mtimes = [p.stat().st_mtime for p in entry.iterdir() if p.is_file()]
            if not mtimes or (now - max(mtimes)) > ttl:
                shutil.rmtree(entry, ignore_errors=True)
                deleted += 1
        except OSError:
            continue
    return deleted
