"""External data fetching: URL contents + Google search snippets."""

from __future__ import annotations

import re
from urllib.parse import unquote

import httpx

URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
_REALTIME_TRIGGERS = (
    "지금", "현재", "오늘", "최근", "최신", "방금", "며칠",
    "news", "today", "latest", "current", "price", "시세",
    "환율", "날씨", "주가", "코스피", "코스닥", "비트코인",
    "뉴스", "경기", "스코어", "일정",
)


def needs_external_data(user_message: str) -> bool:
    if not user_message:
        return False
    if URL_RE.search(user_message):
        return True
    low = user_message.lower()
    return any(trig.lower() in low for trig in _REALTIME_TRIGGERS)


def strip_html(html: str, limit: int = 2000) -> str:
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fetch_url(url: str, timeout: float = 8.0) -> str:
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (vibe-coding fetcher)"},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            body = resp.text
    except Exception as exc:  # noqa: BLE001
        return f"[URL 가져오기 실패: {exc}]"
    if "html" in ctype.lower():
        return strip_html(body, limit=2500)
    return body[:2500]


def google_search(query: str, timeout: float = 8.0) -> list[dict[str, str]]:
    """Parse top snippets from Google search HTML."""
    results: list[dict[str, str]] = []
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0 Safari/537.36",
                "Accept-Language": "ko,en;q=0.8",
            },
        ) as client:
            resp = client.get(
                "https://www.google.com/search",
                params={"q": query, "hl": "ko", "num": "5"},
            )
            resp.raise_for_status()
            html = resp.text
    except Exception:  # noqa: BLE001
        return results

    for match in re.finditer(
        r'<a[^>]+href="/url\?q=(https?://[^&"]+)[^"]*"[^>]*>',
        html,
    ):
        url = unquote(match.group(1))
        if "google.com" in url or "googleapis.com" in url:
            continue
        if any(r["url"] == url for r in results):
            continue
        title = ""
        title_match = re.search(
            r'<h3[^>]*>([\s\S]*?)</h3>',
            html[match.start():match.start() + 500],
        )
        if title_match:
            title = strip_html(title_match.group(1), limit=120)
        if not title:
            title = url
        results.append({"title": title, "snippet": "", "url": url})
        if len(results) >= 5:
            break

    snippets: list[str] = []
    for match in re.finditer(
        r'<div[^>]+(?:class="[^"]*(?:VwiC3b|IsZvec|s3v9rd)[^"]*"|data-sncf="[^"]*")[^>]*>([\s\S]*?)</div>',
        html,
    ):
        text = strip_html(match.group(1), limit=300)
        if text and len(text) > 20:
            snippets.append(text)
        if len(snippets) >= 5:
            break

    for i, r in enumerate(results):
        if i < len(snippets):
            r["snippet"] = snippets[i]
    return results


def fetch_external_context(user_message: str) -> str:
    """Detect URLs / realtime keywords and build a reference block."""
    if not needs_external_data(user_message):
        return ""

    parts: list[str] = []

    urls = URL_RE.findall(user_message)[:2]
    for url in urls:
        body = fetch_url(url)
        parts.append(f"[페이지: {url}]\n{body}")

    if not urls:
        results = google_search(user_message)
        source = "Google 검색"
        if results:
            top = results[0]
            top_body = fetch_url(top["url"])
            lines: list[str] = [f"[{source} 상위 결과]"]
            for i, r in enumerate(results, 1):
                t = r.get("title", "")
                s = r.get("snippet", "")
                u = r.get("url", "")
                lines.append(f"{i}. {t}\n   {s}\n   ({u})")
            lines.append("")
            lines.append(f"[상위 결과 본문 발췌: {top.get('url','')}]")
            lines.append(top_body)
            parts.append("\n".join(lines))

    if not parts:
        return ""

    return (
        "--- 참고 자료 ---\n"
        + "\n\n".join(parts)
        + "\n--- /참고 자료 ---"
    )
