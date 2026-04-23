"""End-to-end test for the 6 newly-added features."""
from playwright.sync_api import sync_playwright

errors = []
INJECTED_MARKDOWN = (
    "Page:\n\n"
    "```html\n"
    '<h1 style="color:tomato">HELLO INLINE SIM</h1>\n'
    "```\n\n"
    "Math: $E=mc^2$\n\n"
    "Diagram:\n\n"
    "```mermaid\n"
    "graph TD; A-->B; B-->C\n"
    "```\n"
)

INJECT_JS = """(content) => {
  const chat = createChat();
  chat.messages.push(buildMessage('user', 'INLINE TEST'));
  chat.messages.push(buildMessage('assistant', content));
  renderMessages();
}"""

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.on("pageerror", lambda e: errors.append(("pageerror", str(e))))
    page.on(
        "console",
        lambda m: errors.append(("console.error", m.text)) if m.type == "error" else None,
    )
    page.goto("http://127.0.0.1:8765/")
    page.wait_for_load_state("networkidle")
    page.fill("#login-username", "admin")
    page.fill("#login-password", "admin")
    page.click("#login-submit")
    page.wait_for_function(
        'document.getElementById("login-overlay").classList.contains("hidden")',
        timeout=5000,
    )
    print("LOGIN OK")

    # --- Memory tab ---
    page.click("#settings-btn")
    page.wait_for_selector("#settings-modal.open")
    page.click("button.settings-tab[data-tab=memory]")
    page.wait_for_timeout(400)
    print("MEMORY SECTION VISIBLE:", page.is_visible("#section-memory"))
    page.click("#memory-add-btn")
    inputs = page.query_selector_all("#memory-list input")
    print("MEMORY INPUTS:", len(inputs))
    if inputs:
        inputs[-1].fill("playwright test memory")
        page.click("#memory-save-btn")
        page.wait_for_timeout(800)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    # --- Inject assistant message with code/math/mermaid ---
    page.evaluate(INJECT_JS, INJECTED_MARKDOWN)
    page.wait_for_timeout(800)

    inline_btn = page.query_selector(".run-inline-btn")
    print("INLINE RUN BTN:", bool(inline_btn))
    if inline_btn:
        inline_btn.click()
        page.wait_for_selector(".inline-sim iframe", timeout=3000)
        page.wait_for_timeout(800)
        try:
            frame = page.frame_locator(".inline-sim iframe").first
            print("INLINE SIM TEXT:", frame.locator("h1").first.inner_text())
        except Exception as e:
            print("INLINE SIM FRAME ERR:", e)

    page.wait_for_timeout(2000)
    print("MERMAID SVG PRESENT:", bool(page.query_selector(".mermaid-block svg")))
    print("KATEX PRESENT:", bool(page.query_selector(".katex")))

    # --- Search snippet (sidebar may be auto-collapsed; expand first) ---
    page.evaluate('() => document.getElementById("sidebar").classList.remove("collapsed")')
    page.wait_for_timeout(200)
    page.fill("#search-input", "INLINE")
    page.wait_for_timeout(400)
    snip = page.query_selector(".chat-item-snippet mark")
    print("SEARCH SNIPPET MARK:", snip.inner_text() if snip else "none")
    page.fill("#search-input", "")

    # --- Stop button ---
    page.evaluate(
        "() => { isGenerating = true; currentAbortController = new AbortController(); updateSendBtn(); }"
    )
    page.wait_for_timeout(150)
    print(
        "SEND BTN HAS STOP CLASS:",
        page.eval_on_selector("#send-btn", 'b => b.classList.contains("stop")'),
    )
    page.click("#send-btn")
    page.wait_for_timeout(150)
    aborted = page.evaluate(
        "() => currentAbortController && currentAbortController.signal.aborted"
    )
    print("AbortController aborted on click:", aborted)
    page.evaluate("() => { isGenerating = false; updateSendBtn(); }")

    # --- Mobile responsive ---
    page.set_viewport_size({"width": 400, "height": 800})
    page.wait_for_timeout(300)
    print("MOBILE BTN VISIBLE:", page.is_visible("#mobile-menu-btn"))
    page.click("#mobile-menu-btn")
    page.wait_for_timeout(200)
    print(
        "SIDEBAR mobile-open:",
        page.eval_on_selector("#sidebar", 's => s.classList.contains("mobile-open")'),
    )
    page.click("#sidebar-backdrop", position={"x": 350, "y": 400}, force=True)
    page.wait_for_timeout(200)
    print(
        "SIDEBAR closed on backdrop:",
        not page.eval_on_selector("#sidebar", 's => s.classList.contains("mobile-open")'),
    )

    print("JS_ERRORS:", len(errors))
    for kind, msg in errors[:8]:
        print(" -", kind, "|", msg[:200])
    browser.close()
print("DONE")
