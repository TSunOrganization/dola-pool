"""Verify whether an account profile's login session is valid.

Usage: python verify_login.py <account-name>
The core logic is in browser.check_login_state (shared with the panel's "Verify" button).
"""
import asyncio
import sys

from browser import check_login_state


async def main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    ok = await check_login_state(account)
    print(f"[{account}] Login session: {'✓ valid' if ok else '✗ invalid'}")


if __name__ == "__main__":
    asyncio.run(main())