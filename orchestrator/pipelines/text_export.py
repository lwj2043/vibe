"""텍스트 본문을 6 가지 포맷으로 변환·저장.

Brainrot text-service (C 코드) 의 contract 를 Python 으로 재구현.

지원 포맷: ``txt``, ``md``, ``html``, ``docx``, ``csv``, ``json``.
호출자는 ``write_text_file`` 결과 ``Path`` 를 받아 ``download_tokens.issue_token`` 으로 토큰 발급.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = ("txt", "md", "html", "docx", "csv", "json")
MAX_INPUT_BYTES = 2 * 1024 * 1024  # 2 MB

Format = Literal["txt", "md", "html", "docx", "csv", "json"]


def list_text_formats() -> list[dict[str, str]]:
    return [
        {"format": "txt", "description": "UTF-8 일반 텍스트"},
        {"format": "md", "description": "Markdown 원본 그대로"},
        {"format": "html", "description": "Markdown → HTML (한글 친화 CSS 포함)"},
        {"format": "docx", "description": "MS Word 문서 (한글 폰트 호환)"},
        {"format": "csv", "description": "GFM 테이블 또는 라인 단위"},
        {"format": "json", "description": "유효 JSON 이면 정렬, 아니면 {content: ...} 래핑"},
    ]


HTML_HEAD = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif;
       max-width: 780px; margin: 2em auto; padding: 0 1em; line-height: 1.6; color: #1a1a1a; }}
h1, h2, h3 {{ font-weight: 600; }}
code {{ font-family: "JetBrains Mono", Consolas, monospace; background: #f4f4f4;
        padding: 0.05em 0.3em; border-radius: 3px; }}
pre {{ background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px;
       padding: 12px; overflow-x: auto; }}
pre code {{ background: none; padding: 0; }}
table {{ border-collapse: collapse; }}
th, td {{ border: 1px solid #d0d7de; padding: 6px 10px; }}
th {{ background: #f6f8fa; }}
blockquote {{ border-left: 4px solid #dfe2e5; color: #555; padding: 0 1em; margin: 0.6em 0; }}
</style>
</head>
<body>
"""

HTML_TAIL = "\n</body></html>\n"


def _to_html(content: str, title: str) -> str:
    try:
        import markdown as md_lib  # type: ignore
    except ImportError:
        body = "<pre>" + (
            content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ) + "</pre>"
    else:
        body = md_lib.markdown(
            content,
            extensions=["extra", "sane_lists", "tables", "fenced_code"],
            output_format="html5",
        )
    return HTML_HEAD.format(title=_escape_title(title)) + body + HTML_TAIL


def _escape_title(title: str) -> str:
    return (
        title.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _to_docx(content: str, target: Path) -> None:
    from docx import Document  # type: ignore
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Malgun Gothic"
    style.font.size = Pt(11)

    in_code = False
    code_lang = ""
    for raw in content.split("\n"):
        line = raw.rstrip("\r")
        fence = re.match(r"^```(\w*)\s*$", line)
        if fence:
            in_code = not in_code
            code_lang = fence.group(1) if in_code else ""
            continue

        # 헤딩 (in_code 일 땐 무시)
        if not in_code:
            heading = re.match(r"^(#{1,6})\s+(.+)$", line)
            if heading:
                level = min(len(heading.group(1)), 4)
                doc.add_heading(heading.group(2).strip(), level=level)
                continue
            if line.strip() == "":
                doc.add_paragraph("")
                continue

        para = doc.add_paragraph(line)
        if in_code:
            for run in para.runs:
                run.font.name = "Consolas"
                run.font.size = Pt(10)

    doc.save(str(target))


_GFM_TABLE_ROW = re.compile(r"^\|(.+)\|$")
_GFM_SEP = re.compile(r"^\|?\s*[:\- |]+\s*\|?$")


def _to_csv(content: str, target: Path) -> None:
    lines = [line for line in content.splitlines() if line.strip()]
    rows: list[list[str]]
    table = _parse_gfm_table(lines)
    if table:
        rows = table
    else:
        rows = [[line] for line in lines]

    with target.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


def _parse_gfm_table(lines: list[str]) -> list[list[str]] | None:
    """GFM 테이블이면 row list 반환, 아니면 None."""
    table_lines = [line.strip() for line in lines if _GFM_TABLE_ROW.match(line.strip())]
    if len(table_lines) < 2:
        return None
    if not _GFM_SEP.match(table_lines[1].replace("|", " ").strip()):
        # second line is not a separator → 일반 테이블 마크업이 아닐 수 있음
        # 그래도 첫 번째 줄이 헤더면 받아들임
        pass
    rows = []
    for line in table_lines:
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)
    # 분리선 행 제거 (--- 만 있는 행)
    return [row for row in rows if not all(re.match(r"^[:\-]+$", c or "") for c in row)]


def _to_json(content: str, target: Path) -> None:
    try:
        parsed = json.loads(content)
        text = json.dumps(parsed, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, ValueError):
        text = json.dumps({"content": content}, ensure_ascii=False, indent=2)
    target.write_text(text + "\n", encoding="utf-8")


def write_text_file(
    content: str,
    format: Format,
    filename_base: str = "document",
    title: str | None = None,
) -> Path:
    """``content`` 를 임시 파일에 저장하고 경로 반환.

    호출자는 ``download_tokens.issue_token(path)`` 으로 토큰 발급 후 즉시 응답.
    """
    if format not in SUPPORTED_FORMATS:
        raise ValueError(f"지원하지 않는 포맷: {format}")
    if len(content.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError(f"input too large (>{MAX_INPUT_BYTES} bytes)")

    from .download_tokens import sanitize_filename
    safe_stem = sanitize_filename(filename_base, default_stem="document")
    if "." in safe_stem:
        safe_stem = safe_stem.rsplit(".", 1)[0]
    safe_title = (title or safe_stem).strip() or safe_stem

    tmp_dir = Path(tempfile.mkdtemp(prefix="vibe-export-"))
    target = tmp_dir / f"{safe_stem}.{format}"

    if format in ("txt", "md"):
        target.write_text(content, encoding="utf-8")
    elif format == "html":
        target.write_text(_to_html(content, safe_title), encoding="utf-8")
    elif format == "docx":
        _to_docx(content, target)
    elif format == "csv":
        _to_csv(content, target)
    elif format == "json":
        _to_json(content, target)
    else:  # pragma: no cover
        raise AssertionError("unreachable")

    return target
