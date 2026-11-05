"""Preview panel regression test.

This covers the bug where the run pane becomes blank after closing it or
closing the whole preview panel and opening the preview again.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from contextlib import closing
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright


INJECTED_MARKDOWN = """
```index.html
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>Preview Regression</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: sans-serif; }
    #marker { color: rgb(37, 99, 235); }
  </style>
</head>
<body>
  <h1 id="marker">PREVIEW_OK</h1>
  <script>document.body.dataset.ready = "yes";</script>
</body>
</html>
```
""".strip()


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(url: str) -> None:
    deadline = time.time() + 15
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=1) as res:
                if res.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"server did not start: {last_error}")


def run_flow(base_url: str) -> None:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1365, "height": 768})
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.goto(base_url)
        page.wait_for_load_state("networkidle")

        page.fill("#login-username", "admin")
        page.fill("#login-password", "admin")
        page.click("#login-submit")
        page.wait_for_function(
            'document.getElementById("login-overlay").classList.contains("hidden")',
            timeout=5000,
        )

        page.evaluate(
            """content => {
              resetState();
              const chat = createChat();
              chat.messages.push(buildMessage('assistant', content));
              STATE.currentChatId = chat.id;
              renderMessages();
            }""",
            INJECTED_MARKDOWN,
        )

        page.click(".preview-btn")
        page.wait_for_function('document.getElementById("preview-panel").classList.contains("open")')
        expect(page.frame_locator("#preview-frame").locator("#marker")).to_have_text("PREVIEW_OK")

        page.click(".run-pane-close")
        expect(page.locator("#show-run-btn")).to_be_visible()
        page.click("#show-run-btn")
        expect(page.frame_locator("#preview-frame").locator("#marker")).to_have_text("PREVIEW_OK")

        page.click("#preview-panel > .preview-header .icon-btn")
        page.wait_for_function('!document.getElementById("preview-panel").classList.contains("open")')
        page.click(".preview-btn")
        expect(page.frame_locator("#preview-frame").locator("#marker")).to_have_text("PREVIEW_OK")

        browser.close()

    relevant_errors = [
        err for err in errors
        if "favicon" not in err and "DevTools" not in err
    ]
    assert not relevant_errors, relevant_errors[:5]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="start a temporary uvicorn server")
    parser.add_argument("--url", default="")
    args = parser.parse_args()

    proc: subprocess.Popen[str] | None = None
    try:
        if args.serve:
          port = free_port()
          proc = subprocess.Popen(
              [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port)],
              stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL,
              text=True,
          )
          base_url = f"http://127.0.0.1:{port}/"
          wait_for_server(base_url)
        else:
          base_url = args.url or "http://127.0.0.1:8100/"
        run_flow(base_url)
        print("preview panel e2e passed")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
