"""Combined preview builder.

Parses code generation output (```파일경로 ... ``` fenced blocks) into a
``{path: content}`` map, then merges HTML/CSS/JS files into a single
self-contained HTML document that can be rendered in a headless browser
for screenshot-based visual review.
"""

from __future__ import annotations

import re
from typing import Any

# ```<first-line>\n<body>``` — firstLine may be "index.html" or "html" etc.
_FENCE_RE = re.compile(r"```([^\n]*)\n([\s\S]*?)```", re.MULTILINE)

_HTML_EXTS = (".html", ".htm")
_CSS_EXTS = (".css",)
_JS_EXTS = (".js", ".mjs", ".cjs")


def extract_code_blocks(text: str) -> dict[str, str]:
    """Pull every fenced block whose first-line token looks like a file path.

    Non-path fences (e.g. ```json```, ```bash```) are ignored.
    """
    files: dict[str, str] = {}
    for match in _FENCE_RE.finditer(text or ""):
        header = (match.group(1) or "").strip().split()[0] if match.group(1).strip() else ""
        body = match.group(2) or ""
        if not header or "." not in header:
            continue
        # header like "index.html" or "src/App.jsx"
        files[header] = body.rstrip() + ("\n" if not body.endswith("\n") else "")
    return files


def _pick_entry_html(files: dict[str, str]) -> str | None:
    """index.html 을 최우선, 그 외 .html 은 경로 짧은 것."""
    html_paths = [p for p in files if p.lower().endswith(_HTML_EXTS)]
    if not html_paths:
        return None
    for preferred in ("index.html", "index.htm"):
        for p in html_paths:
            if p.lower().endswith(preferred) and (
                p == preferred or p.lower().rsplit("/", 1)[-1] == preferred
            ):
                return p
    html_paths.sort(key=lambda p: (p.count("/"), len(p)))
    return html_paths[0]


def _inline_css_links(html: str, files: dict[str, str]) -> str:
    """<link rel="stylesheet" href="X.css"> → <style>...</style> (파일이 map 에 있을 때만)."""
    def _sub(match: re.Match) -> str:
        href = match.group(1)
        key = _resolve_key(files, href, _CSS_EXTS)
        if not key:
            return match.group(0)
        return f"<style>\n/* inlined: {key} */\n{files[key]}\n</style>"

    pattern = re.compile(
        r'<link[^>]+href=["\']([^"\']+\.css)["\'][^>]*>',
        re.IGNORECASE,
    )
    return pattern.sub(_sub, html)


def _inline_script_srcs(html: str, files: dict[str, str]) -> str:
    """<script src="X.js"></script> → <script>...</script>."""
    def _sub(match: re.Match) -> str:
        src = match.group(1)
        key = _resolve_key(files, src, _JS_EXTS)
        if not key:
            return match.group(0)
        return f"<script>\n/* inlined: {key} */\n{files[key]}\n</script>"

    pattern = re.compile(
        r'<script[^>]+src=["\']([^"\']+)["\'][^>]*>\s*</script>',
        re.IGNORECASE,
    )
    return pattern.sub(_sub, html)


def _resolve_key(
    files: dict[str, str], href: str, allowed_exts: tuple[str, ...]
) -> str | None:
    """href 를 files 의 실제 키로 매핑. 상대경로/절대경로/대소문자 관대."""
    if not href:
        return None
    cleaned = href.split("?", 1)[0].split("#", 1)[0].lstrip("./").lstrip("/")
    if not cleaned.lower().endswith(allowed_exts):
        return None
    if cleaned in files:
        return cleaned
    lower = cleaned.lower()
    for key in files:
        if key.lower() == lower:
            return key
        if key.lower().endswith("/" + lower):
            return key
    return None


def _append_orphan_assets(
    html: str, files: dict[str, str], used: set[str]
) -> str:
    """참조되지 않은 .css/.js 도 검토를 위해 말미에 주입한다.

    일부 모델은 index.html 에 <link>/<script> 를 빠뜨리고 별도 파일만 뽑아냅니다.
    그 경우에도 '완성된 화면'을 한 번에 보기 위해 남은 자원을 </body> 앞에 주입합니다.
    """
    extras_css: list[str] = []
    extras_js: list[str] = []
    for path, body in files.items():
        if path in used:
            continue
        low = path.lower()
        if low.endswith(_CSS_EXTS):
            extras_css.append(f"<style>\n/* auto-included: {path} */\n{body}\n</style>")
        elif low.endswith(_JS_EXTS):
            extras_js.append(
                f"<script>\n/* auto-included: {path} */\n{body}\n</script>"
            )
    if not extras_css and not extras_js:
        return html

    injection = "\n".join(extras_css + extras_js) + "\n"
    if re.search(r"</body\s*>", html, re.IGNORECASE):
        return re.sub(
            r"</body\s*>", injection + "</body>", html, count=1, flags=re.IGNORECASE
        )
    # </body> 가 없으면 끝에 붙임
    return html + "\n" + injection


def build_combined_html(files: dict[str, str]) -> str | None:
    """HTML 기반 프로젝트를 하나의 self-contained 문서로 병합.

    반환값:
    - 병합 성공: 단일 HTML 문자열
    - HTML 엔트리 없음: None (screenshot 스킵 대상)
    """
    if not files:
        return None

    entry = _pick_entry_html(files)
    if entry is None:
        return None

    used: set[str] = {entry}
    html = files[entry]

    # 1) <link href="X.css"> 를 map 의 파일로 치환
    def _collect_css(match: re.Match) -> str:
        key = _resolve_key(files, match.group(1), _CSS_EXTS)
        if key:
            used.add(key)
        return match.group(0)

    re.sub(
        r'<link[^>]+href=["\']([^"\']+\.css)["\'][^>]*>',
        _collect_css,
        html,
        flags=re.IGNORECASE,
    )
    html = _inline_css_links(html, files)

    # 2) <script src="X.js">
    def _collect_js(match: re.Match) -> str:
        key = _resolve_key(files, match.group(1), _JS_EXTS)
        if key:
            used.add(key)
        return match.group(0)

    re.sub(
        r'<script[^>]+src=["\']([^"\']+)["\'][^>]*>\s*</script>',
        _collect_js,
        html,
        flags=re.IGNORECASE,
    )
    html = _inline_script_srcs(html, files)

    # 3) 참조되지 않은 .css/.js 도 말미에 주입 (모델이 링크를 빠뜨린 경우)
    html = _append_orphan_assets(html, files, used)

    return html


def describe_file_map(files: dict[str, str]) -> str:
    """검토 프롬프트에 넣을 '파일 목록' 한 줄 요약."""
    if not files:
        return "(파일 없음)"
    lines = [f"- {path} ({len(body)} bytes)" for path, body in files.items()]
    return "\n".join(lines)


def summarize_for_review(files: dict[str, str]) -> str:
    """검토 프롬프트에 파일별 전체 내용을 그대로 넘길 용도."""
    parts: list[str] = []
    for path, body in files.items():
        parts.append(f"```{path}\n{body}```")
    return "\n\n".join(parts)


__all__ = [
    "extract_code_blocks",
    "build_combined_html",
    "describe_file_map",
    "summarize_for_review",
]
