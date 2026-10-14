"""Functional tests for the Dola skill.md support and per-proxy quota rotation.

The real project depends on patchright / aiohttp / OpenCV / Pillow, which are not
installed in this environment. We stub those modules before importing the project
so the pool logic can be exercised in isolation. Run with:

    python3 tests/test_skill_and_rotation.py
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import types
from datetime import date
from pathlib import Path


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, item):
        return _Dummy()

    def __call__(self, *args, **kwargs):
        return _Dummy()


# --- stub the heavy optional dependencies before importing the project ---
_stub("aiohttp", ClientSession=_Dummy, ClientTimeout=_Dummy, ClientResponse=_Dummy)
_patchright = _stub("patchright")
_stub("patchright.async_api", async_playwright=_Dummy)
_patchright.async_api = sys.modules["patchright.async_api"]
_stub("cv2", imdecode=_Dummy, IMREAD_COLOR=1, IMREAD_UNCHANGED=-1, Canny=_Dummy,
      cvtColor=_Dummy, COLOR_BGR2GRAY=1, matchTemplate=_Dummy, TM_CCOEFF_NORMED=1,
      minMaxLoc=_Dummy)
_stub("numpy", frombuffer=_Dummy, uint8="uint8")
_stub("PIL")
_stub("PIL.Image", open=_Dummy)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DOLA_DAILY_LIMIT", "10")

import config  # noqa: E402
import browser_pool  # noqa: E402
import media  # noqa: E402
from video_worker_ui import AccountLimitedError  # noqa: E402


def _make_accounts(root: str, *names: str):
    for name in names:
        (Path(root) / name).mkdir(parents=True, exist_ok=True)


def test_daily_limit_from_env():
    assert config.DAILY_LIMIT == 10
    assert browser_pool.DAILY_LIMIT == 10
    print("ok: DAILY_LIMIT honours DOLA_DAILY_LIMIT (10)")


def test_skill_helpers():
    path = media.write_skill_markdown("# my skill\n", "task1")
    assert path.name == "skill.md"
    assert path.read_text(encoding="utf-8").startswith("# my skill")
    shutil.rmtree(path.parent, ignore_errors=True)
    assert media.resolve_skill_markdown("inline skill") == "inline skill"
    print("ok: write_skill_markdown / resolve_skill_markdown")


def test_usage_table_migration():
    root = tempfile.mkdtemp()
    db = os.path.join(root, "pool.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE usage (account TEXT, day TEXT, used INTEGER, PRIMARY KEY(account, day))")
    con.execute("INSERT INTO usage VALUES ('acc1', ?, 2)", (date.today().isoformat(),))
    con.commit()
    con.close()

    pool = browser_pool.BrowserPool(accounts_dir=root, db_path=db, max_concurrency=1, proxies=["p1"])
    columns = {row[1] for row in pool._conn.execute("PRAGMA table_info(usage)")}
    assert "proxy" in columns, columns
    migrated = pool._conn.execute(
        "SELECT used FROM usage WHERE account='acc1' AND proxy=''").fetchone()
    assert migrated[0] == 2, migrated
    print("ok: legacy usage table migrated to add the proxy column")


async def test_rotation_resets_budget_on_new_proxy():
    root = tempfile.mkdtemp()
    _make_accounts(root, "acc1", "acc2")
    calls = []

    async def fake_generate(account, prompt, ratio, duration, **kwargs):
        calls.append((account, kwargs.get("proxy"), kwargs.get("skill_path")))
        if len(calls) == 1:
            raise AccountLimitedError("daily limit reached")
        return {"video_url": "u", "local_path": "/x.mp4",
                "conversation_id": "1", "account": account}

    browser_pool.generate_video = fake_generate
    pool = browser_pool.BrowserPool(accounts_dir=root, db_path=os.path.join(root, "u.db"),
                                    max_concurrency=1, proxies=["p1", "p2"])
    result = await pool.generate_video("hi", skill_path="/tmp/skill.md")

    assert calls[0] == ("acc1", "p1", "/tmp/skill.md"), calls
    assert calls[1] == ("acc1", "p2", "/tmp/skill.md"), calls
    assert result["account"] == "acc1"
    assert pool._proxy_for("acc1") == "p2"
    assert pool.used_today("acc1") == 1
    usage = {row["proxy"]: row["used"] for row in pool._conn.execute(
        "SELECT proxy, used FROM usage WHERE account='acc1'")}
    assert usage == {"p1": 10, "p2": 1}, usage
    print("ok: daily limit rotates the proxy and gives the account a fresh budget")


async def test_single_proxy_switches_account_and_marks_limited():
    root = tempfile.mkdtemp()
    _make_accounts(root, "acc1", "acc2")
    calls = []

    async def fake_generate(account, prompt, ratio, duration, **kwargs):
        calls.append((account, kwargs.get("proxy")))
        if account == "acc1":
            raise AccountLimitedError("daily limit reached")
        return {"video_url": "u", "local_path": "/x.mp4",
                "conversation_id": "1", "account": account}

    browser_pool.generate_video = fake_generate
    pool = browser_pool.BrowserPool(accounts_dir=root, db_path=os.path.join(root, "u.db"),
                                    max_concurrency=1, proxies=["p1"])
    result = await pool.generate_video("hi")

    assert result["account"] == "acc2"
    assert calls[0] == ("acc1", "p1") and calls[1] == ("acc2", "p1"), calls
    status = {a["name"]: a for a in pool.list_accounts()}
    assert status["acc1"]["rate_limited"] is True
    assert status["acc1"]["used_today"] == 10
    print("ok: with a single proxy the account is marked limited and the pool switches account")


def test_store_persists_skill_markdown():
    from store import TaskStore
    root = tempfile.mkdtemp()
    store = TaskStore(os.path.join(root, "tasks.db"))
    store.create("video_1", "seedance-2.5", "prompt", "16:9", 30, skill_markdown="# skill")
    row = store.get("video_1")
    assert row["skill_markdown"] == "# skill", row
    print("ok: store persists skill_markdown for task recovery")


def main():
    test_daily_limit_from_env()
    test_skill_helpers()
    test_usage_table_migration()
    asyncio.run(test_rotation_resets_budget_on_new_proxy())
    asyncio.run(test_single_proxy_switches_account_and_marks_limited())
    test_store_persists_skill_markdown()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
