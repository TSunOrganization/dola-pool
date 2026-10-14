"""Check whether the conversation sent via the UI actually produced a video.

Open /chat, click into the latest history conversation, and see whether there is a <video> or a failure message.
"""
import asyncio
import json
import sys

from patchright.async_api import async_playwright

from browser import launch_account_context


async def main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"

    async with async_playwright() as p:
        context = await launch_account_context(p, account)
        page = context.pages[0] if context.pages else await context.new_page()

        # Listen to the network: capture the status of /chat/completion and /im/chain/single
        page.on("response", lambda r: print(f"  [net] {r.status} {r.url[:100]}", flush=True)
                if any(k in r.url for k in ("/chat/completion", "im/chain", "verify", "captcha")) else None)

        await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        await page.screenshot(path="check_01_home.png")

        # Find the sidebar history conversation (the one containing "海边日落").
        # Keep the "海边日落" literal as-is: it matches a specific existing conversation title.
        target = await page.query_selector("text=海边日落")
        if not target:
            print("✗ Did not find the \"seaside sunset\" conversation in history")
            await context.close()
            return
        await target.click()
        await page.wait_for_timeout(6000)
        await page.screenshot(path="check_02_conv.png")
        print("url:", page.url)

        info = await page.evaluate("""() => {
            const videos = [...document.querySelectorAll('video')].map(v => ({src: (v.currentSrc||v.src||'').slice(0,200)}));
            const texts = [...document.querySelectorAll('div,p,span')].map(e => e.childElementCount===0 ? (e.textContent||'').trim() : '').filter(t => t && t.length>2 && t.length<200);
            return {videos, texts: texts.slice(-15)};
        }""")
        print("videos:", json.dumps(info["videos"], ensure_ascii=False))
        print("Tail text:")
        for t in info["texts"]:
            print("   |", t)
        await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        import traceback
        traceback.print_exc()