"""Video-generation worker: reuses the login session of an accounts/<account> profile,
submits video generation via an in-page fetch, polls /im/chain/single for the result,
and downloads it to downloads/ once a download_url is obtained.

All protocol fields come from the reverse-engineering in dola_client.py
(_build_video_body / _poll_video); the transport layer is replaced with a
"fetch inside a real browser page" to evade slider risk control.

Usage: python video_worker.py <account_name> "prompt" [ratio] [duration_seconds]
Example: python video_worker.py acc1 "A cat chasing butterflies on the grass" 9:16 5

Acceptance criteria: one complete generation, obtaining a dola CDN download_url
plus a local video file.
"""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import aiohttp
from patchright.async_api import async_playwright

import config
from browser import cookie_value, launch_account_context
from dola_client import CREDIT_FAIL_PATTERN, CreditError
from video_probe import SUBMIT_JS

# Poll /im/chain/single (fields aligned with dola_client._request_im_chain / _poll_video)
# Note: Content-Type must include encoding=utf-8, and agw-js-conv must be str (not "str, str")
POLL_JS = r"""
async ({conversationId, msToken, fp}) => {
  // Current protocol as of 2026-08: the body must wrap uplink_body.pull_singe_chain_uplink_body,
  // anchor_index=MAX_SAFE_INTEGER + direction=1 to pull the latest; channel=2, version="1".
  // (The flat body in dola_client.py is the old protocol and returns 712010202)
  const params = new URLSearchParams({
    version_code: "20800", language: "ja", device_platform: "web",
    doubao_device_platform: "web", aid: "495671", real_aid: "495671",
    pkg_type: "release_version", pc_version: "3.32.61", doubao_pc_version: "3.32.61",
    region: "JP", sys_region: "JP", samantha_web: "1", web_platform: "browser",
    "use-olympus-account": "1", web_tab_id: crypto.randomUUID(),
  });

  const resp = await fetch("/im/chain/single?" + params.toString(), {
    method: "POST",
    headers: {
      "Content-Type": "application/json; encoding=utf-8",
      "agw-js-conv": "str",
      "Accept": "*/*",
    },
    body: JSON.stringify({
      cmd: 3100,
      uplink_body: {
        pull_singe_chain_uplink_body: {
          conversation_id: conversationId,
          anchor_index: Number.MAX_SAFE_INTEGER,
          conversation_type: 3,
          direction: 1,
          limit: 20,
          ext: {},
          filter: {index_list: []},
          evaluate_ab_params: "",
          evaluate_common_params: "",
        },
      },
      sequence_id: crypto.randomUUID(),
      channel: 2,
      version: "1",
    }),
    credentials: "include",
  });
  if (!resp.ok) return {ok: false, status: resp.status, texts: [], videos: []};

  const data = await resp.json();
  const messages =
    (((data.downlink_body || {}).pull_singe_chain_downlink_body) || {}).messages || [];
  const texts = [];
  const videos = [];
  const videoModels = [];
  for (const msg of messages) {
    let content = msg.content;
    if (typeof content === "string") {
      try { content = JSON.parse(content); } catch (e) { continue; }
    }
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      const text = (((block.content || {}).text_block) || {}).text || "";
      if (text) texts.push(text.slice(0, 120));
      if (block.block_type !== 2074) continue;
      const creations = (((block.content || {}).creation_block) || {}).creations || [];
      for (const cre of creations) {
        if (cre.type !== 2) continue;
        const url = ((cre.video || {}).download_url) || "";
        if (url.startsWith("http")) {
          videos.push(url);
          videoModels.push((cre.video || {}).video_model || "");
        }
      }
    }
  }
  return {ok: true, status: resp.status, texts, videos, videoModels};
}
"""


def extract_unwatermarked_url(video_model_str: str, fallback_url: str) -> str:
    """Extract the watermark-free video URL (base64) from video_model.video_list.*.main_url.

    The watermark-free version and the regular download_url are differently signed
    links to the same content; the query parameter is lr=unwatermarked
    (the regular link uses lr=cici_ai).
    Falls back to download_url on failure.
    """
    try:
        vm = json.loads(video_model_str or "{}")
        video_list = vm.get("video_list") or {}
        candidates = []
        for v in video_list.values():
            if not isinstance(v, dict):
                continue
            main_url = v.get("main_url") or ""
            if not main_url:
                continue
            try:
                decoded = base64.b64decode(main_url).decode("utf-8", "ignore")
            except Exception:
                continue
            if decoded.startswith("http"):
                candidates.append((int(v.get("bitrate") or v.get("real_bitrate") or 0), decoded))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
    except Exception:
        pass
    return fallback_url


class RiskControlError(Exception):
    """Slider/risk control triggered (710022004 slide / 710022002 rate limit); this account+IP must cool down or be switched"""


def _check_submit(result: dict) -> str:
    """Check the SUBMIT_JS result and return the conversation_id; raise directly on risk control/errors."""
    status = result.get("status")
    if status != 200:
        raise RiskControlError(f"submission failed HTTP {status}: {json.dumps(result.get('events', []), ensure_ascii=False)[:300]}")

    for err in result.get("errors", []):
        if "710022004" in err or "slide" in err or "shark" in err:
            raise RiskControlError(f"slider risk control triggered: {err[:300]}")
        if "710022002" in err:
            raise RiskControlError(f"rate limit triggered: {err[:300]}")
        raise Exception(f"submission returned an error event: {err[:300]}")

    conv_id = result.get("convId") or ""
    if not conv_id:
        raise Exception(
            "video submission did not return a conversation_id: "
            + json.dumps(result.get("events", []), ensure_ascii=False)[:300]
        )
    return conv_id


async def _download(url: str, account: str) -> Path:
    """Download the video to DOWNLOAD_DIR (via the proxy; the CDN may also be region-restricted). Returns the local path."""
    dl_dir = Path(config.DOWNLOAD_DIR)
    dl_dir.mkdir(parents=True, exist_ok=True)
    fname = dl_dir / f"{account}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, proxy=config.PROXY or None) as resp:
            resp.raise_for_status()
            with open(fname, "wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    f.write(chunk)
    return fname


async def generate_video(account: str, prompt: str, ratio: str = "9:16",
                         duration: int = 5, timeout: int = None) -> dict:
    """Full generation flow: submit -> poll -> download.

    Returns {"video_url": dola CDN temporary link, "local_path": local file, "conversation_id": ...}
    Exceptions: RiskControlError (risk control/rate limit), CreditError (insufficient credit, switch account),
         TimeoutError (timed out without a video), FileNotFoundError (profile does not exist)
    """
    timeout = timeout or config.VIDEO_TIMEOUT
    async with async_playwright() as p:
        context = await launch_account_context(p, account)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            cookies = await context.cookies("https://www.dola.com")
            sessionid = cookie_value(cookies, "sessionid")
            if not sessionid:
                raise CreditError(f"{account} login session is invalid (no sessionid); rerun login.py {account}")
            ms_token = cookie_value(cookies, "msToken")
            fp = cookie_value(cookies, "s_v_web_id")

            print(f"[{account}] submitting video generation: {prompt[:40]} | {ratio} | {duration}s", flush=True)
            # evaluate has no timeout parameter, so use wait_for as a fallback (returns once the SSE stream is fully read)
            result = await asyncio.wait_for(page.evaluate(
                SUBMIT_JS,
                {"prompt": prompt, "ratio": ratio, "duration": duration,
                 "msToken": ms_token, "fp": fp},
            ), timeout=180)
            conv_id = _check_submit(result)
            print(f"[{account}] accepted conversation_id={conv_id}, polling for the video...", flush=True)

            start = time.time()
            while time.time() - start < timeout:
                await asyncio.sleep(5)
                try:
                    poll = await asyncio.wait_for(page.evaluate(
                        POLL_JS,
                        {"conversationId": conv_id, "msToken": ms_token, "fp": fp},
                    ), timeout=30)
                except Exception as e:
                    print(f"[{account}] poll error (continuing to retry): {e}", flush=True)
                    continue
                if not poll.get("ok"):
                    continue

                for text in poll.get("texts", []):
                    if CREDIT_FAIL_PATTERN.search(text):
                        raise CreditError(f"insufficient credit: {text[:80]}")

                videos = poll.get("videos", [])
                if videos:
                    url = videos[0]
                    print(f"[{account}] video ready download_url={url[:100]}...", flush=True)
                    local = await _download(url, account)
                    print(f"[{account}] downloaded {local} ({local.stat().st_size / 1e6:.1f} MB)", flush=True)
                    return {
                        "video_url": url,
                        "local_path": str(local),
                        "conversation_id": conv_id,
                        "account": account,
                    }
                print(f"[{account}] ...generating (waited {int(time.time() - start)}s)", flush=True)

            raise TimeoutError(f"no video within {timeout}s (conversation_id={conv_id})")
        finally:
            await context.close()


async def _main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    prompt = sys.argv[2] if len(sys.argv) > 2 else "A cat chasing butterflies on the grass"
    ratio = sys.argv[3] if len(sys.argv) > 3 else "9:16"
    duration = int(sys.argv[4]) if len(sys.argv) > 4 else 5

    try:
        result = await generate_video(account, prompt, ratio, duration)
    except (RiskControlError, CreditError, TimeoutError) as e:
        print(f"\nfailed: {e}")
        sys.exit(1)
    print("\n=== Result ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())