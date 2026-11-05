"""중앙 로깅 설정.

- 콘솔 핸들러: 기존 동작 유지
- 회전 파일 핸들러: ``logs/server.log`` 5MB × 5개

서버 시작 시 한 번만 ``configure_logging()`` 을 호출한다.
``pipelines.dual_model_pipeline`` 에서 호출하던 ``logging.basicConfig`` 은
이미 핸들러가 등록된 경우 no-op 이 되므로 이 설정이 우선한다.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"


def configure_logging(
    level: int = logging.INFO,
    log_dir: Path = _DEFAULT_LOG_DIR,
    file_name: str = "server.log",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_vibe_root_handler", False) for h in root.handlers):
        return  # 이미 설정됨

    root.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._vibe_root_handler = True  # type: ignore[attr-defined]
    root.addHandler(console)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / file_name,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler._vibe_root_handler = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)
    except OSError:
        # 파일 핸들러 실패는 치명적이지 않음 — 콘솔만 사용
        pass

    # 서드파티 노이즈 줄이기
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
