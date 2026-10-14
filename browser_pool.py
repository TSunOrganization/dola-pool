"""Browser-based account pool: scans accounts/ profiles, per-account daily quota, mutual exclusion per account.

v2: accounts_meta metadata table (scheduling switch/notes/login-state cache/risk-control cooldown); the panel reads and writes the same data.
Quota counts are stored in SQLite (pool_usage.db), counted conservatively: a count is recorded only on a successful video or a credit error.
The quota is configurable (config.DAILY_LIMIT, default 2; up to 10 with the Dola skill system) and is tracked per egress proxy, so switching the exit IP (VPN) resets an account's budget.
"""
import asyncio
import shutil
import sqlite3
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from dola_client import CreditError
from video_worker_ui import (
    AccountLimitedError, CreditInsufficientError, RiskControlError, generate_video, resume_video,
)
import config

DAILY_LIMIT = config.DAILY_LIMIT  # videos per account per day (2 free, up to 10 with the skill system)
COOLDOWN_SEC = 1800  # risk-control cooldown of 30 minutes


class AllAccountsLimitedError(RuntimeError):
    """All accounts with scheduling enabled have reached the Dola daily video limit."""


class AllAccountsQuotaBlockedError(RuntimeError):
    """All accounts with scheduling enabled are known to have insufficient credit."""


class BrowserPool:
    def __init__(self, accounts_dir: str = "accounts", db_path: str = "pool_usage.db",
                 max_concurrency: int = 1, proxies: list[str] | None = None):
        self.accounts_dir = Path(accounts_dir)
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self._locks: dict[str, asyncio.Lock] = {}
        # Egress proxies used for IP rotation; the Dola quota is tied to the exit IP.
        self._proxies: list[str] = list(proxies) if proxies is not None else list(config.PROXIES)
        self._proxy_index: dict[str, int] = {}
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS usage (account TEXT, day TEXT, proxy TEXT DEFAULT '', "
            "used INTEGER, PRIMARY KEY(account, day, proxy))"
        )
        self._migrate_usage_table()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts_meta (
                name TEXT PRIMARY KEY,
                scheduling INTEGER DEFAULT 1,
                note TEXT DEFAULT '',
                email TEXT DEFAULT '',
                created_at REAL,
                last_used_at REAL DEFAULT 0,
                login_ok INTEGER,
                login_checked_at REAL DEFAULT 0,
                cooldown_until REAL DEFAULT 0,
                rate_limited_until REAL DEFAULT 0,
                limit_reason TEXT DEFAULT '',
                quota_blocked_until REAL DEFAULT 0,
                quota_reason TEXT DEFAULT '',
                credit_balance INTEGER,
                credit_checked_at REAL DEFAULT 0
            )
            """
        )
        self._conn.commit()
        # Backward compatibility with old databases: add the new metadata columns
        for column, definition in (
            ("email", "TEXT DEFAULT ''"),
            ("rate_limited_until", "REAL DEFAULT 0"),
            ("limit_reason", "TEXT DEFAULT ''"),
            ("quota_blocked_until", "REAL DEFAULT 0"),
            ("quota_reason", "TEXT DEFAULT ''"),
            ("credit_balance", "INTEGER"),
            ("credit_checked_at", "REAL DEFAULT 0"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE accounts_meta ADD COLUMN {column} {definition}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

    def _migrate_usage_table(self):
        """Old databases used PRIMARY KEY(account, day); add the proxy column so each egress IP keeps its own count."""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(usage)")}
        if "proxy" in columns:
            return
        self._conn.execute("ALTER TABLE usage RENAME TO usage_old")
        self._conn.execute(
            "CREATE TABLE usage (account TEXT, day TEXT, proxy TEXT DEFAULT '', "
            "used INTEGER, PRIMARY KEY(account, day, proxy))"
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO usage (account, day, proxy, used) "
            "SELECT account, day, '', used FROM usage_old"
        )
        self._conn.execute("DROP TABLE usage_old")
        self._conn.commit()

    # ===== Egress proxy rotation =====

    def _proxy_for(self, account: str) -> str | None:
        """Return the egress proxy currently assigned to an account (None when no proxy is configured)."""
        if not self._proxies:
            return None
        index = self._proxy_index.get(account, 0)
        if index >= len(self._proxies):
            index = 0
        return self._proxies[index]

    def _proxy_key(self, account: str) -> str:
        return self._proxy_for(account) or ""

    def _rotate_proxy(self, account: str) -> bool:
        """Switch an account to the next egress proxy. Returns False when there is no next proxy."""
        if len(self._proxies) <= 1:
            return False
        index = self._proxy_index.get(account, 0)
        if index + 1 >= len(self._proxies):
            return False
        self._proxy_index[account] = index + 1
        return True

    def _clear_rate_limit(self, account: str):
        """Clear the daily-limit mark for an account (used after rotating to a fresh egress IP)."""
        self._conn.execute(
            "UPDATE accounts_meta SET rate_limited_until=0, limit_reason='' WHERE name=?",
            (account,),
        )
        self._conn.commit()

    # ===== Account discovery / metadata =====

    def _ensure_meta(self, name: str):
        self._conn.execute(
            "INSERT OR IGNORE INTO accounts_meta (name, created_at) VALUES (?, ?)",
            (name, time.time()),
        )
        self._conn.commit()

    @property
    def accounts(self) -> list:
        if not self.accounts_dir.exists():
            return []
        names = sorted(d.name for d in self.accounts_dir.iterdir()
                       if d.is_dir() and not d.name.startswith("."))
        for n in names:
            self._ensure_meta(n)
        return names

    def _meta(self, name: str):
        return self._conn.execute(
            "SELECT * FROM accounts_meta WHERE name=?", (name,)).fetchone()

    def used_today(self, account: str) -> int:
        """Videos already produced today on the account's current egress proxy."""
        row = self._conn.execute(
            "SELECT used FROM usage WHERE account=? AND day=? AND proxy=?",
            (account, date.today().isoformat(), self._proxy_key(account)),
        ).fetchone()
        return row[0] if row else 0

    def _claim(self, account: str):
        self._conn.execute(
            "INSERT INTO usage(account, day, proxy, used) VALUES (?,?,?,1) "
            "ON CONFLICT(account, day, proxy) DO UPDATE SET used=used+1",
            (account, date.today().isoformat(), self._proxy_key(account)),
        )
        self._conn.commit()

    def _cap_usage(self, account: str, value: int):
        """Force today's count for the current proxy up to at least `value` (used when Dola reports the daily limit)."""
        self._conn.execute(
            "INSERT INTO usage(account, day, proxy, used) VALUES (?,?,?,?) "
            "ON CONFLICT(account, day, proxy) DO UPDATE SET used=MAX(used, excluded.used)",
            (account, date.today().isoformat(), self._proxy_key(account), value),
        )
        self._conn.commit()

    def _next_limit_reset(self) -> float:
        """Compute the next daily-quota reset time (default: 00:00 the next day, Japan time)."""
        try:
            tz = ZoneInfo(config.LIMIT_RESET_TZ)
        except Exception:
            # When tzdata is not installed in the runtime, fall back to a fixed offset for common Asian timezones.
            offsets = {"Asia/Tokyo": 9, "Asia/Hong_Kong": 8, "UTC": 0}
            tz = timezone(timedelta(hours=offsets.get(config.LIMIT_RESET_TZ, 9)))
        now = datetime.now(tz)
        next_day = now.date() + timedelta(days=1)
        return datetime.combine(next_day, dt_time.min, tzinfo=tz).timestamp()

    def _clear_expired_rate_limits(self):
        now = time.time()
        cur = self._conn.execute(
            "UPDATE accounts_meta SET rate_limited_until=0, limit_reason='', "
            "quota_blocked_until=0, quota_reason='' "
            "WHERE (rate_limited_until > 0 AND rate_limited_until <= ?) "
            "OR (quota_blocked_until > 0 AND quota_blocked_until <= ?)", (now, now))
        if cur.rowcount:
            self._conn.commit()

    def _mark_quota_blocked(self, account: str, reason: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=?, last_used_at=? WHERE name=?",
            (self._next_limit_reset(), reason[:300], time.time(), account),
        )
        self._conn.commit()

    def _mark_daily_limit(self, account: str, reason: str = ""):
        """Dola explicitly returned the daily limit: cap the count and mark it until the reset time, skipping this account entirely in the meantime."""
        self._cap_usage(account, DAILY_LIMIT)
        self._conn.execute(
            "UPDATE accounts_meta SET last_used_at=?, rate_limited_until=?, limit_reason=? WHERE name=?",
            (time.time(), self._next_limit_reset(), reason[:300], account),
        )
        self._conn.commit()

    def list_accounts(self) -> list:
        """Panel view: merged meta + quota + rate-limit status + busy flag."""
        self._clear_expired_rate_limits()
        now = time.time()
        out = []
        for a in self.accounts:
            m = self._meta(a)
            used = self.used_today(a)
            lock = self._locks.get(a)
            out.append({
                "name": a,
                "proxy": self._proxy_for(a),
                "proxy_index": self._proxy_index.get(a, 0),
                "scheduling": bool(m["scheduling"]) if m else True,
                "note": m["note"] if m else "",
                "email": m["email"] if m else "",
                "created_at": m["created_at"] if m else 0,
                "last_used_at": m["last_used_at"] if m else 0,
                "login_ok": m["login_ok"] if m else None,
                "login_checked_at": m["login_checked_at"] if m else 0,
                "cooldown_until": m["cooldown_until"] if m else 0,
                "cooling": bool(m and m["cooldown_until"] > now),
                "rate_limited_until": m["rate_limited_until"] if m and m["rate_limited_until"] else 0,
                "rate_limited": bool(m and m["rate_limited_until"] > now),
                "limit_reason": m["limit_reason"] if m else "",
                "quota_blocked_until": m["quota_blocked_until"] if m and m["quota_blocked_until"] else 0,
                "quota_blocked": bool(m and m["quota_blocked_until"] > now),
                "quota_reason": m["quota_reason"] if m else "",
                "credit_balance": m["credit_balance"] if m else None,
                "credit_checked_at": m["credit_checked_at"] if m else 0,
                "used_today": used,
                "limit": DAILY_LIMIT,
                "remaining": max(0, DAILY_LIMIT - used),
                "busy": bool(lock and lock.locked()),
            })
        return out

    def set_scheduling(self, name: str, on: bool):
        self._conn.execute(
            "UPDATE accounts_meta SET scheduling=? WHERE name=?", (1 if on else 0, name))
        self._conn.commit()

    def set_email(self, name: str, email: str):
        self._conn.execute(
            "UPDATE accounts_meta SET email=? WHERE name=?", (email, name))
        self._conn.commit()

    def set_login_status(self, name: str, ok: bool):
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=? WHERE name=?",
            (1 if ok else 0, time.time(), name),
        )
        self._conn.commit()

    def set_note(self, name: str, note: str):
        self._conn.execute(
            "UPDATE accounts_meta SET note=? WHERE name=?", (note, name))
        self._conn.commit()

    def delete_account(self, name: str):
        lock = self._locks.get(name)
        if lock and lock.locked():
            raise RuntimeError("account is generating a video and cannot be deleted")
        d = self.accounts_dir / name
        if d.exists():
            shutil.rmtree(d)
        self._conn.execute("DELETE FROM accounts_meta WHERE name=?", (name,))
        self._conn.commit()

    async def verify_account(self, name: str) -> bool:
        """Headlessly verify the login state and write it back to the cache. Raises RuntimeError if the account is busy."""
        if name not in self.accounts:
            raise FileNotFoundError(f"profile does not exist: {name}")
        lock = self._locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("account is generating a video; verify later")
        from browser import check_login_state
        ok = await check_login_state(name)
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=? WHERE name=?",
            (1 if ok else 0, time.time(), name),
        )
        self._conn.commit()
        return ok

    # ===== Scheduling =====

    def _set_credit_balance(self, account: str, balance: int, source: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET credit_balance=?, credit_checked_at=? WHERE name=?",
            (max(0, int(balance)), time.time(), account),
        )
        if balance < 2:
            self._conn.execute(
                "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=? WHERE name=?",
                (self._next_limit_reset(), source[:300] or "insufficient credit", account),
            )
        self._conn.commit()

    def _credit_available(self, account: str, required: int = 2) -> bool:
        row = self._meta(account)
        return not row or row["credit_balance"] is None or row["credit_balance"] >= required

    def _schedulable(self, a: dict) -> bool:
        return (a["scheduling"] and not a["cooling"] and not a["rate_limited"]
                and not a["quota_blocked"] and a["used_today"] < DAILY_LIMIT
                and (a["credit_balance"] is None or a["credit_balance"] >= 2))

    @property
    def all_accounts_limited(self) -> bool:
        """All accounts with scheduling enabled and not in risk-control cooldown have reached the daily limit."""
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["rate_limited"] or a["used_today"] >= DAILY_LIMIT for a in candidates
        )

    @property
    def all_accounts_quota_blocked(self) -> bool:
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["quota_blocked"] or a["rate_limited"] or a["used_today"] >= DAILY_LIMIT
            for a in candidates
        ) and any(a["quota_blocked"] for a in candidates)

    @property
    def available(self) -> bool:
        return any(self._schedulable(a) for a in self.list_accounts())

    @property
    def cookie_count(self) -> int:  # backward compatibility with the old /health field
        return len(self.accounts)

    def account_status(self) -> list:
        return [{
            "account": a["name"], "used_today": a["used_today"], "limit": a["limit"],
            "rate_limited": a["rate_limited"], "rate_limited_until": a["rate_limited_until"],
            "quota_blocked": a["quota_blocked"], "quota_blocked_until": a["quota_blocked_until"],
        } for a in self.list_accounts()]

    async def resume_video(self, account: str, conversation_id: str, timeout: int,
                           on_poll=None) -> dict:
        """Resume an already-accepted conversation; it does not take part in account selection and is not skipped due to the account's current credit status."""
        async with self.semaphore:
            lock = self._locks.setdefault(account, asyncio.Lock())
            async with lock:
                def on_balance(balance, source=""):
                    self._set_credit_balance(account, balance, source)
                try:
                    result = await resume_video(account, conversation_id, timeout,
                                                on_poll=on_poll, on_balance=on_balance,
                                                proxy=self._proxy_for(account))
                    self._claim(account)
                    self._conn.execute(
                        "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                        (time.time(), account))
                    self._conn.commit()
                    return result
                except TimeoutError:
                    self._claim(account)
                    self._conn.commit()
                    raise

    async def generate_video(self, prompt: str, ratio: str = None, duration: int = None,
                             model: str = "seedance_v2.0", on_conversation_id=None,
                             on_poll=None, on_balance=None,
                             reference_image_paths: list[str] | None = None,
                             skill_path: str | None = None) -> dict:
        """Pick a schedulable and idle account to generate a video.

        Automatically switches accounts on insufficient credit / risk control
        (risk-controlled accounts enter cooldown). When an account hits the Dola
        daily limit and more than one egress proxy is configured, it rotates to
        the next proxy instead of giving up: the quota is tied to the egress IP,
        so a new exit IP gives the account a fresh budget.
        """
        async with self.semaphore:
            last_err = None
            for a in self.list_accounts():
                if not self._schedulable(a):
                    continue
                account = a["name"]
                lock = self._locks.setdefault(account, asyncio.Lock())
                # Concurrent tasks skip accounts that are already in use, to avoid multiple requests queuing on the same profile.
                if lock.locked():
                    continue
                async with lock:
                    if not self._schedulable(next(x for x in self.list_accounts() if x['name'] == account)):
                        continue  # state changed while waiting
                    # Retry the same account across egress proxies; each proxy has its own daily budget.
                    for _ in range(max(1, len(self._proxies))):
                        try:
                            def on_balance(balance, source=""):
                                self._set_credit_balance(account, balance, source)

                            result = await generate_video(
                                account, prompt, ratio, duration, model=model,
                                on_conversation_id=on_conversation_id, on_poll=on_poll,
                                on_balance=on_balance, reference_image_paths=reference_image_paths,
                                skill_path=skill_path, proxy=self._proxy_for(account))
                            self._claim(account)
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            return result
                        except CreditInsufficientError as e:
                            print(f"[pool] {account} insufficient credit before generation, skipping: {e}", flush=True)
                            self._mark_quota_blocked(account, str(e))
                            last_err = e
                            break
                        except AccountLimitedError as e:
                            # The daily quota is tied to the egress IP: cap this proxy's count and,
                            # if another proxy is available, rotate and retry the same account.
                            self._cap_usage(account, DAILY_LIMIT)
                            if self._rotate_proxy(account):
                                print(f"[pool] {account} hit the daily limit on the current egress, "
                                      f"rotating proxy -> {self._proxy_for(account)}", flush=True)
                                self._clear_rate_limit(account)
                                last_err = e
                                continue
                            print(f"[pool] {account} reached the daily limit, switching account immediately: {e}", flush=True)
                            self._mark_daily_limit(account, str(e))
                            last_err = e
                            break
                        except CreditError as e:
                            print(f"[pool] {account} insufficient credit, switching account: {e}", flush=True)
                            self._claim(account)
                            last_err = e
                            break
                        except RiskControlError as e:
                            print(f"[pool] {account} risk control, cooling down for 30 minutes, switching account: {e}", flush=True)
                            self._conn.execute(
                                "UPDATE accounts_meta SET cooldown_until=? WHERE name=?",
                                (time.time() + COOLDOWN_SEC, account))
                            self._conn.commit()
                            last_err = e
                            break
                        except TimeoutError as e:
                            # After the request has obtained a conversation_id, Dola may still be generating.
                            # Never switch accounts and resubmit just because local polling timed out, or credit will be double-charged / the video produced twice.
                            self._claim(account)
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            raise
                        except FileNotFoundError as e:
                            print(f"[pool] {account} profile missing, skipping: {e}", flush=True)
                            last_err = e
                            break
            if self.all_accounts_quota_blocked:
                raise AllAccountsQuotaBlockedError(
                    f"429: all accounts with scheduling enabled are known to have insufficient credit: {last_err or 'no account'}"
                )
            if self.all_accounts_limited:
                raise AllAccountsLimitedError(
                    f"429: all accounts with scheduling enabled have reached the Dola daily video limit: {last_err or 'no account'}"
                )
            raise RuntimeError(f"no usable account in the pool (scheduling off/cooldown/credit exhausted): {last_err or 'no account'}")