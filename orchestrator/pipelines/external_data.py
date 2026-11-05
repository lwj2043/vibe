"""External data fetching: URL contents + Google search snippets.

URL 본문 추출은 ``pipelines.web_extract.fetch_as_markdown`` 으로 위임 (SSRF 가드 + readability 추출).
실패 시 강제로 단순 HTTP fallback 으로 복귀하지 않고 에러 메시지를 컨텍스트에 포함.
"""

from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import unquote

import httpx

from . import web_extract

URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
_REALTIME_TRIGGERS = (
    "지금", "현재", "오늘", "최근", "최신", "방금", "며칠",
    "news", "today", "latest", "current", "price", "시세",
    "환율", "날씨", "주가", "코스피", "코스닥", "비트코인",
    "뉴스", "경기", "스코어", "일정",
)
_WEEKDAYS_KO = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _format_datetime_ko(now: datetime) -> str:
    weekday = _WEEKDAYS_KO[now.weekday()]
    return f"{now.year}년 {now.month}월 {now.day}일 {weekday}"


def current_datetime_reply(user_message: str, now: datetime | None = None) -> str | None:
    """Return a deterministic answer for direct current date/time questions."""
    if not user_message:
        return None

    text = user_message.lower()
    asks_date = any(
        token in text
        for token in (
            "오늘 몇일",
            "오늘 며칠",
            "오늘 날짜",
            "날짜",
            "몇월 몇일",
            "몇 월 몇 일",
            "무슨 요일",
            "몇 요일",
            "요일",
            "today",
            "current date",
            "what date",
        )
    )
    asks_time = any(
        token in text
        for token in (
            "몇시",
            "몇 시",
            "현재 시간",
            "지금 시간",
            "현재시각",
            "현재 시각",
            "what time",
            "current time",
        )
    )
    if not asks_date and not asks_time:
        return None

    now = now or _local_now()
    date_text = _format_datetime_ko(now)
    if asks_time:
        return f"현재 시각은 {date_text} {now.hour}시 {now.minute}분입니다."
    return f"오늘은 {date_text}입니다."


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


def fetch_url(url: str, timeout: float = 10.0, char_limit: int = 4000) -> str:
    """URL 본문을 Markdown 으로 가져와 char_limit 까지 잘라서 반환.

    web_extract 실패 시 단순 HTTP fallback (SSRF 가드 없음) 은 사용하지 않는다 —
    악성 사용자가 강제로 사설 IP fetch 를 유도할 수 있기 때문.
    """
    try:
        result = web_extract.fetch_as_markdown(url, timeout=timeout)
    except web_extract.WebFetchError as exc:
        return f"[URL 가져오기 실패: {exc}]"
    except Exception as exc:  # noqa: BLE001
        return f"[URL 가져오기 실패: {exc}]"
    md = result.get("markdown", "")
    return md[:char_limit] if char_limit > 0 else md


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
    date_reply = current_datetime_reply(user_message)
    if date_reply:
        parts.append(f"[현재 날짜/시간]\n{date_reply}")

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
