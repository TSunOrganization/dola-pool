"""Unified startup for the patchright persistent context: explicit proxy + anti-detection args.

The proxy must be passed explicitly (do not rely on the system proxy — a change in system proxy
rules once routed dola to a direct connection that got blocked; see DEVELOPMENT.md section 5).
All scripts that need to open dola go through here.
"""
from pathlib import Path

import config


LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]


async def launch_account_context(p, account: str, headless: bool = None, use_extension: bool = False,
                                 proxy: str = None):
    """Launch the accounts/<account> profile and return the BrowserContext. The caller is responsible for close.

    p: async_playwright() instance
    headless: None = use config.HEADLESS
    proxy: explicit egress proxy for this launch; None = use config.PROXY
    """
    profile_dir = Path("accounts") / account
    if not profile_dir.exists():
        raise FileNotFoundError(
            f"Account profile does not exist: {profile_dir} (run python login.py {account} first)"
        )
    launch_headless = config.HEADLESS if headless is None else headless
    args = list(LAUNCH_ARGS)
    if use_extension:
        if not config.EXTENSION_ENABLED:
            raise RuntimeError("Dola extension is disabled (DOLA_EXTENSION_ENABLED=0)")
        extension_dir = Path(config.EXTENSION_DIR).resolve()
        if not extension_dir.exists():
            raise FileNotFoundError(f"Dola extension directory does not exist: {extension_dir}")
        # The Chrome debugger API extension requires headed mode; the extension intercepts skill/action-bar responses.
        launch_headless = False
        args.extend([
            f"--disable-extensions-except={extension_dir}",
            f"--load-extension={extension_dir}",
        ])
    kwargs = {
        "headless": launch_headless,
        "args": args,
        "locale": "ja-JP",
        "timezone_id": "Asia/Tokyo",
    }
    chosen_proxy = config.PROXY if proxy is None else proxy
    if chosen_proxy:
        kwargs["proxy"] = {"server": chosen_proxy}
    return await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)


def cookie_value(cookies: list, name: str) -> str:
    """Get the value of the specified cookie from the context.cookies() result"""
    return next((c["value"] for c in cookies if c["name"] == name and c["value"]), "")

async def check_login_state(account: str) -> bool:
    """Open dola headless and return whether the login session is valid (sessionid + input box present).

    Shared by the verify_login.py CLI and the panel's "Verify" button.
    """
    from patchright.async_api import async_playwright
    async with async_playwright() as p:
        context = await launch_account_context(p, account)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            cookies = await context.cookies("https://www.dola.com")
            if not cookie_value(cookies, "sessionid"):
                return False
            return bool(await page.evaluate(
                """() => !!(document.querySelector('textarea')
                        || document.querySelector('[contenteditable="true"]')
                        || document.querySelector('input[type="text"]'))"""
            ))
        finally:
            await context.close()
