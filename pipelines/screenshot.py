"""Headless rendering for visual review.

Takes a standalone HTML string and returns a PNG screenshot as bytes, so the
review step can feed it to the VLM via the OpenAI ``image_url`` content type.

Playwright is an optional dependency:
- If not installed → ``render_html_screenshot`` returns ``None`` and the
  pipeline falls back to text-only review.
- To enable visual review:
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 800
_RENDER_TIMEOUT_MS = 15_000


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def render_html_screenshot(
    html: str,
    width: int = DEFAULT_VIEWPORT_WIDTH,
    height: int = DEFAULT_VIEWPORT_HEIGHT,
    full_page: bool = True,
) -> bytes | None:
    """후방 호환: PNG bytes 만 반환."""
    result = render_html_with_diagnostics(html, width=width, height=height, full_page=full_page)
    return result[0] if result else None


def render_html_with_diagnostics(
    html: str,
    width: int = DEFAULT_VIEWPORT_WIDTH,
    height: int = DEFAULT_VIEWPORT_HEIGHT,
    full_page: bool = True,
) -> tuple[bytes, list[str]] | None:
    """Render ``html`` and return ``(png_bytes, runtime_errors)`` or ``None``.

    ``runtime_errors`` 는 헤드리스 렌더링 중 잡힌 JS 콘솔 에러 + 페이지 예외 메시지
    리스트. 없으면 빈 리스트.

    ``None`` 반환 조건:
    - Playwright (또는 Chromium) 미설치
    - 렌더 자체가 실패 (타임아웃 등)
    """
    if not html:
        return None

    try:
        from playwright.sync_api import Error as PlaywrightError  # type: ignore
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.info(
            "playwright 미설치 — 시각 검토 스킵. "
            "활성화: `pip install playwright && playwright install chromium`"
        )
        return None

    errors: list[str] = []

    def _truncate(msg: str, limit: int = 300) -> str:
        return msg if len(msg) <= limit else msg[:limit] + "…"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                )
                page = context.new_page()
                page.set_default_timeout(_RENDER_TIMEOUT_MS)

                def _on_console(msg):
                    if msg.type in ("error", "warning"):
                        errors.append(_truncate(f"[console.{msg.type}] {msg.text}"))

                def _on_pageerror(exc):
                    errors.append(_truncate(f"[pageerror] {exc}"))

                def _on_requestfailed(req):
                    # 외부 리소스 실패는 소음이 많아 제외, 단 상대경로/자체 리소스만 포함
                    url = req.url
                    if url.startswith("data:") or url.startswith("blob:"):
                        return
                    errors.append(
                        _truncate(f"[requestfailed] {url} — {req.failure}")
                    )

                page.on("console", _on_console)
                page.on("pageerror", _on_pageerror)
                page.on("requestfailed", _on_requestfailed)

                try:
                    page.set_content(html, wait_until="networkidle")
                except PlaywrightError:
                    page.set_content(html, wait_until="domcontentloaded")
                page.wait_for_timeout(500)
                png = page.screenshot(full_page=full_page, type="png")
                return png, errors
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("스크린샷 렌더 실패: %s", exc)
        return None


__all__ = [
    "playwright_available",
    "render_html_screenshot",
    "render_html_with_diagnostics",
]
