"""Playwright automated browser probing script.

Verifies whether the patchright anti-detection browser + system global proxy can access dola.com
normally (not blocked / not flagged by anti-bot).
Uses headful mode (with a UI) for easy visual confirmation. When run, it opens a browser to dola,
saves a screenshot, and prints page information.
"""
import asyncio
import sys

from patchright.async_api import async_playwright


async def main():
    headless = "--headless" in sys.argv
    async with async_playwright() as p:
        # Under a global proxy, Chromium follows the system proxy by default; do not pass proxy explicitly
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",  # remove automation-control fingerprints
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        context = await browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = await context.new_page()

        # Print key fingerprints to confirm the anti-detection is working
        webdriver = await page.evaluate("navigator.webdriver")
        print(f"navigator.webdriver = {webdriver}  (True=detected as automation, False/undefined=normal)")

        try:
            await page.goto("https://www.dola.com", timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"Failed to open dola: {e}")
            await browser.close()
            return

        await page.wait_for_timeout(6000)
        print("URL  :", page.url)
        print("TITLE:", await page.title())

        # Check for signs of anti-bot / verification popups
        html = await page.content()
        flags = {
            "slider container (captcha)": "captcha" in html.lower(),
            "verify check": "verify" in html.lower(),
            "login button": "登录" in html or "Log in" in html or "Sign in" in html,  # "登录" = Chinese for "log in"; keep literal for UI matching
        }
        for k, v in flags.items():
            print(f"  [{ '✓' if v else ' ' }] {k}")

        await page.screenshot(path="probe_screenshot.png", full_page=False)
        print("Screenshot saved: probe_screenshot.png")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
