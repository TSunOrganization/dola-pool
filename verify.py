"""dola cookie verification + video smoke test tool.

Usage:
  python verify.py "full-cookie-string"    # pass the cookie directly
  python verify.py                         # read the first non-comment line from cookies.txt

Prerequisite: the HTTPS_PROXY environment variable must point to a JP/KR exit proxy (dola.com geo-block)
"""
import os
import sys
import asyncio

from dola_client import DolaClient


def _load_cookie(argv) -> str:
    if len(argv) > 1:
        return argv[1]
    if os.path.exists("cookies.txt"):
        with open("cookies.txt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    return ""


async def main():
    cookie = _load_cookie(sys.argv)
    if not cookie:
        print("No cookie provided. Usage: python verify.py \"<full-cookie>\" or fill in cookies.txt")
        return

    client = DolaClient(cookie)

    print("[1/2] Verifying cookie (chat liveness probe)...")
    try:
        reply = await client.chat([{"role": "user", "content": "Introduce yourself in one sentence"}])
        print(f"      chat OK -> {str(reply)[:100]}")
    except Exception as e:
        print(f"      chat FAIL -> {type(e).__name__}: {e}")
        print("      The cookie may be expired or the proxy is not working; please check.")
        return

    print("[2/2] Video smoke test (seedance_v2.0 / 3s / 9:16)...")
    try:
        url = await client.generate_video("A cat chasing butterflies on the grass", ratio="9:16", duration=3)
        print(f"      video OK -> {url}")
    except Exception as e:
        print(f"      video FAIL -> {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
