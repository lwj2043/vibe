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
    """Render ``html`` in headless Chromium and return a PNG bytes, or ``None``.

    Returns ``None`` when:
    - Playwright (or the Chromium binary) is not installed
    - Rendering fails for any reason (timeout, JS error, etc.)
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
                # set_content 는 네트워크 자원이 올 필요는 없지만 CDN JS(React 등) 가
                # 있으면 기다려야 한다. 실패 시 'domcontentloaded' 폴백.
                try:
                    page.set_content(html, wait_until="networkidle")
                except PlaywrightError:
                    page.set_content(html, wait_until="domcontentloaded")
                # 애니메이션/JS 초기화를 위한 짧은 대기
                page.wait_for_timeout(500)
                png = page.screenshot(full_page=full_page, type="png")
                return png
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("스크린샷 렌더 실패: %s", exc)
        return None


__all__ = ["playwright_available", "render_html_screenshot"]
