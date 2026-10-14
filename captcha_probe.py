"""Slider captcha probe: send a message through dola's real UI to trigger and capture the structure of the slider captcha.

Background: a bare in-page fetch submission is blocked by the shark anti-bot with 710022004; on receiving
that error the frontend pops up its own slider UI.
This script opens in headful mode, enters and sends text through the UI, waits for the captcha to appear,
and saves the screenshot/DOM/frame information for analysis.

Usage: python captcha_probe.py <account-name>
Output: captcha_probe.png (screenshot), captcha_probe.html (DOM), console frame list
"""
import asyncio
import sys
from pathlib import Path

from patchright.async_api import async_playwright

from browser import launch_account_context

PROMPT = "Seaside sunset, waves lapping the beach"


async def dump_state(page, context, tag):
    """Save screenshot + DOM + frame list"""
    png = Path(f"captcha_probe_{tag}.png")
    html = Path(f"captcha_probe_{tag}.html")
    await page.screenshot(path=str(png), full_page=False)
    try:
        await html.write_text(await page.content(), encoding="utf-8")
    except Exception as e:
        print(f"  Failed to save html: {e}")
    print(f"[{tag}] url={page.url}")
    print(f"[{tag}] screenshot -> {png}")
    frames = page.frames
    print(f"[{tag}] frames({len(frames)}):")
    for f in frames:
        print(f"    - name={f.name!r} url={f.url[:150]}")


async def main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"

    async with async_playwright() as p:
        context = await launch_account_context(p, account, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await dump_state(page, context, "01_loaded")

        # Find the input box: textarea / contenteditable
        box = await page.query_selector("textarea")
        input_kind = "textarea"
        if not box:
            box = await page.query_selector('[contenteditable="true"]')
            input_kind = "contenteditable"
        if not box:
            print("✗ Input box not found (the login session may have expired)")
            await context.close()
            return
        print(f"Input box: {input_kind}")

        await box.click()
        await page.wait_for_timeout(500)
        await page.keyboard.type(PROMPT, delay=120)  # human-like typing
        await page.wait_for_timeout(800)
        await dump_state(page, context, "02_typed")

        print("Pressing Enter to send...")
        await page.keyboard.press("Enter")

        # Wait for the captcha to appear (up to 30s, checked once per second)
        for i in range(30):
            await page.wait_for_timeout(1000)
            html = await page.content()
            low = html.lower()
            hit = [kw for kw in ("captcha", "verify", "secsdk", "shark", "滑块", "拖动", "slider", "puzzle") if kw in low]
            nframes = len(page.frames)
            if hit or nframes > 1:
                print(f"t+{i+1}s possible captcha detected: keywords={hit} frames={nframes}")
                await page.wait_for_timeout(2000)  # wait for rendering to complete
                await dump_state(page, context, "03_captcha")
                # Further: look for img/canvas in each frame
                for f in page.frames:
                    try:
                        info = await f.evaluate("""() => {
                            const imgs = [...document.images].map(im => ({src: (im.src||'').slice(0,200), w: im.naturalWidth, h: im.naturalHeight}));
                            const canvases = [...document.querySelectorAll('canvas')].map(c => ({w: c.width, h: c.height}));
                            return {imgs, canvases, bodyLen: document.body ? document.body.innerHTML.length : 0};
                        }""")
                        if info["imgs"] or info["canvases"]:
                            print(f"  frame {f.url[:80]}: {info}")
                    except Exception:
                        pass
                break
        else:
            print("No captcha detected within 30s; dumping the final state")
            await dump_state(page, context, "03_final")

        print("\nKeeping the browser open for 60s for visual inspection (you can operate manually)...")
        await page.wait_for_timeout(60000)
        await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        import traceback
        traceback.print_exc()