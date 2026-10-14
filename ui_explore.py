"""Explore the "Create video" UI flow: entry -> controls -> send -> capture the real frontend request.

Goals:
1. Map out the DOM of the video creation panel (ratio/duration/model options)
2. Capture the query/headers/body of the real frontend /chat/completion request to compare with SUBMIT_JS
3. Confirm whether the UI video flow triggers a slider
"""
import asyncio
import json
import sys

from patchright.async_api import async_playwright

from browser import launch_account_context

PROMPT = "An orange tabby cat dozing on the windowsill, sunlight streaming in"
CAPTURE = {}


async def main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"

    async with async_playwright() as p:
        context = await launch_account_context(p, account)
        page = context.pages[0] if context.pages else await context.new_page()

        def on_request(req):
            if "/chat/completion" in req.url:
                CAPTURE["url"] = req.url
                CAPTURE["headers"] = {k: v for k, v in req.headers.items()
                                      if k.lower() not in ("accept-encoding", "cookie")}
                CAPTURE["post"] = req.post_data

        def on_response(resp):
            if any(k in resp.url for k in ("/chat/completion", "verify", "captcha")):
                print(f"  [net] {resp.status} {resp.url[:120]}", flush=True)

        page.on("request", on_request)
        page.on("response", on_response)

        await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        print("Clicking \"Create video\"...")
        await page.click("text=動画を作成")  # "動画を作成" = Japanese for "Create video"; keep the literal, it selects a Dola UI element
        await page.wait_for_timeout(2500)
        await page.screenshot(path="ui_01_panel.png")

        # Dump the text of visible controls near the input area
        controls = await page.evaluate("""() => {
            const els = [...document.querySelectorAll('button, [role="button"], [class*="chip"], [class*="option"], [class*="select"]')];
            return els.map(e => (e.textContent||'').trim()).filter(t => t && t.length < 30).slice(0, 80);
        }""")
        print("Control text:", json.dumps(controls, ensure_ascii=False))

        # Find the input box
        box = await page.query_selector("textarea") or await page.query_selector('[contenteditable="true"]')
        print("Input box:", "textarea" if await page.query_selector("textarea") else "contenteditable")
        await box.click()
        await page.keyboard.type(PROMPT, delay=100)
        await page.wait_for_timeout(1000)
        await page.screenshot(path="ui_02_typed.png")

        print("Pressing Enter to send...")
        await page.keyboard.press("Enter")

        # Wait for: URL change / captcha / 15s
        for i in range(20):
            await page.wait_for_timeout(1000)
            if CAPTURE.get("url"):
                break
        await page.wait_for_timeout(5000)
        await page.screenshot(path="ui_03_sent.png")
        print("url now:", page.url)

        if CAPTURE.get("url"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(CAPTURE["url"]).query)
            print("\n=== Real frontend request query params ===")
            print(json.dumps({k: v[0] for k, v in q.items()}, ensure_ascii=False, indent=1))
            print("\n=== headers (except cookie) ===")
            print(json.dumps(CAPTURE["headers"], ensure_ascii=False, indent=1))
            body = json.loads(CAPTURE["post"])
            print("\n=== key body fields ===")
            print("chat_ability:", json.dumps(body.get("chat_ability"), ensure_ascii=False))
            print("ext:", json.dumps(body.get("ext"), ensure_ascii=False))
            texts = [b.get("content", {}).get("text_block", {}).get("text", "")
                     for m in body.get("messages", []) for b in m.get("content_block", [])]
            print("message text:", texts)
        else:
            print("✗ Did not capture a /chat/completion request")

        await page.wait_for_timeout(8000)
        await page.screenshot(path="ui_04_after.png")
        await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        import traceback
        traceback.print_exc()