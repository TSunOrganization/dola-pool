"""Automatically add an account: automated Google OAuth login (email----password----TOTP secret), saving the profile.

Usage: python add_account.py <account_name> "email----password----totp_secret"

Only logs in and saves the profile; it sends no video requests. Raises RuntimeError on failure (caught by the panel's jobs).
Google pages have anti-automation detection; use headful mode to improve the success rate; on failure it screenshots dbg_add_account.png.
"""
import asyncio
import base64
import hashlib
import hmac
import struct
import sys
import time
from pathlib import Path

from patchright.async_api import async_playwright

from browser import LAUNCH_ARGS
import config


def totp(secret: str, period: int = 30, digits: int = 6) -> str:
    """Standard TOTP (RFC 6238), compatible with Google Authenticator."""
    secret = secret.replace(" ", "").upper()
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    counter = int(time.time() // period)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[19] & 15
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % (10 ** digits)
    return str(code).zfill(digits)


async def google_login(g, email: str, password: str, secret: str):
    """State machine that completes the Google login: account chooser/email/password/2FA/consent page/age confirmation, until it leaves accounts.google.com."""
    for step in range(12):
        await g.wait_for_timeout(2500)
        if "accounts.google.com" not in g.url:
            print("[google] left the Google domain (OAuth redirect back)", flush=True)
            return
        # 1) Account chooser page (decided strictly by URL; the consent page also shows the email and would cause a false match)
        if "accountchooser" in g.url:
            acc = g.locator(f"text={email}").first
            if await acc.count() and await acc.is_visible():
                await acc.click(timeout=5000)
                print("[google] account chooser page -> clicking account", flush=True)
                continue
        # 2) Email page: during transition Google keeps a hidden identifierId, so only visible input fields must be handled.
        identifier = g.locator("#identifierId").first
        if await identifier.count() and await identifier.is_visible():
            await identifier.fill(email)
            # On Google's newer pages a transition overlay occasionally intercepts the mouse; a DOM click safely triggers the same submit event.
            await g.locator("#identifierNext").evaluate("e => e.click()")
            print("[google] email page -> submitting", flush=True)
            await g.wait_for_timeout(1500)
            continue
        # 3) Password page
        pwd = g.locator('input[name="Passwd"]').first
        if await pwd.count() and await pwd.is_visible():
            await g.wait_for_timeout(600)
            await pwd.fill(password)
            await g.locator("#passwordNext").evaluate("e => e.click()")
            print("[google] password page -> submitting", flush=True)
            await g.wait_for_timeout(1500)
            continue
        # 4) OAuth consent page (继续/続行/Continue) -- checked before 2FA; the consent page has no input fields
        clicked = False
        for sel in ["#submit_button",
                    "[role='button']:has-text('続行')", "button:has-text('続行')",
                    "[role='button']:has-text('继续')", "button:has-text('继续')",
                    "[role='button']:has-text('Continue')", "button:has-text('Continue')"]:
            try:
                loc = g.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    print(f"[google] consent page -> clicking {sel}", flush=True)
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            continue
        # 5) 2FA page
        if await g.locator('input[type="tel"]').count():
            if not secret:
                raise RuntimeError("Google requires two-step verification, but no TOTP secret was provided")
            code = totp(secret)
            print(f"[google] 2FA page -> TOTP={code}", flush=True)
            await g.fill('input[type="tel"]', code)
            await g.click("#totpNext")
            continue
        # 6) dola age-confirmation modal (18+) -- dispatch the click directly via JS to bypass selector-matching issues
        if await g.locator("text=18").count():
            ok = await g.evaluate("""() => {
                const els = [...document.querySelectorAll('button, [role="button"], div, span')];
                const t = els.find(e => (e.textContent || '').trim() === 'OK' && e.childElementCount === 0);
                if (t) { t.click(); return true; }
                return false;
            }""")
            print(f"[dola] age confirmation -> JS click OK = {ok}", flush=True)
            await g.wait_for_timeout(1500)
            continue
        txt = await g.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)")
        print(f"[google] step{step} unrecognized page url={g.url[:80]} text={txt[:200]}", flush=True)
    if "accounts.google.com" in g.url:
        await g.screenshot(path="dbg_google2.png")
        raise RuntimeError("Google login did not complete within 12 steps (screenshot dbg_google2.png)")


async def add_account_flow(account: str, email: str, password: str, secret: str) -> bool:
    """Full add-account flow; returns True on success and raises RuntimeError on any failure."""
    profile_dir = Path("accounts") / account
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        kwargs = {"headless": False, "args": LAUNCH_ARGS,
                  "locale": "ja-JP", "timezone_id": "Asia/Tokyo"}
        if config.PROXY:
            kwargs["proxy"] = {"server": config.PROXY}
        context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000)
            await page.wait_for_timeout(4000)

            cookies = await context.cookies("https://www.dola.com")
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                print(f"[{account}] already logged in, no login needed", flush=True)
                return True

            # Click the login entry -> Google option (when logged out, dola automatically shows a semi-modal login dialog)
            try:
                await page.wait_for_selector(".semi-modal-wrap", timeout=10000)
            except Exception:
                await page.locator("text=ログイン").first.click(timeout=10000)
                await page.wait_for_selector(".semi-modal-wrap", timeout=10000)
            await page.locator("text=Googleで続ける").first.click(timeout=10000)

            # The Google page may be in a popup or the current tab
            await page.wait_for_timeout(3000)
            g = next((pg for pg in context.pages if "accounts.google.com" in pg.url), None)
            if g is None and "accounts.google.com" in page.url:
                g = page
            if g is None:
                await page.screenshot(path="dbg_add_account.png")
                raise RuntimeError("did not navigate to the Google login page (screenshot dbg_add_account.png)")

            await google_login(g, email, password, secret)

            # Wait for dola to write the sessionid (after the OAuth redirect back)
            for _ in range(60):
                await page.wait_for_timeout(3000)
                cookies = await context.cookies("https://www.dola.com")
                if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                    print(f"[{account}] ✓ login successful, sessionid written, profile saved to {profile_dir}", flush=True)
                    await page.wait_for_timeout(3000)
                    return True
            await page.screenshot(path="dbg_add_account.png")
            raise RuntimeError("sessionid not obtained within 3 minutes (screenshot dbg_add_account.png)")
        finally:
            await context.close()


async def main():
    account = sys.argv[1]
    email, password, secret = sys.argv[2].split("----")
    await add_account_flow(account, email, password, secret)
    print(f"[{account}] account added; run verify_login.py {account} to double-check")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"✗ {e}")
        sys.exit(1)