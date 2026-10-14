"""Tool for capturing a dola account's login session.

Usage: python login.py <account-name>
Opens a browser to dola.com; you complete the Google login manually.
Once the login session (sessionid) is detected, the profile is automatically saved to
accounts/<account-name>/ and the browser is closed.
Afterward video_worker reuses this profile, so no re-login is needed.
"""
import asyncio
import sys
from pathlib import Path

from patchright.async_api import async_playwright

from browser import LAUNCH_ARGS
import config


async def main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    profile_dir = Path("accounts") / account
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        kwargs = {
            "headless": False,
            "args": LAUNCH_ARGS,
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
        }
        if config.PROXY:
            kwargs["proxy"] = {"server": config.PROXY}
        context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.dola.com/chat", timeout=60000)
        print(f"[{account}] Browser opened; please complete the Google login in the window (just get into dola)...")

        # Poll for the login session: sessionid appears under the dola.com domain
        for _ in range(180):  # up to 15 minutes
            cookies = await context.cookies("https://www.dola.com")
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                print(f"[{account}] Login session (sessionid) detected; waiting for the session to be fully written...")
                await page.wait_for_timeout(4000)
                await context.close()
                print(f"[{account}] OK, profile saved to {profile_dir}")
                return
            await page.wait_for_timeout(5000)

        print(f"[{account}] No login detected within 15 minutes; exiting, please rerun this script")
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())