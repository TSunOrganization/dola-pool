"""dola-pool configuration: everything goes through environment variables, with defaults."""
import os
from pathlib import Path


def _load_local_env():
    """Load ignored .env.local for local/tunnel runs; real environment wins."""
    path = Path(__file__).with_name(".env.local")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()
HOST = os.getenv("DOLA_HOST", "0.0.0.0")
PORT = int(os.getenv("DOLA_PORT", "8000"))

# Service-facing API keys (comma-separated for multiple; empty = no auth, for intranet debugging only)
API_KEYS = [k.strip() for k in os.getenv("DOLA_API_KEYS", "").split(",") if k.strip()]

# Account pool cookie file (one dola.com cookie per line)
COOKIES_FILE = os.getenv("DOLA_COOKIES_FILE", "cookies.txt")

# Max number of concurrent video tasks (controls concurrency, avoids anti-bot triggers / same-account concurrency)
MAX_CONCURRENCY = int(os.getenv("DOLA_MAX_CONCURRENCY", "3"))

# Global limit on pending tasks (queued + processing), 0 = unlimited.
MAX_PENDING_TASKS = int(os.getenv("DOLA_MAX_PENDING_TASKS", "100"))

# Video generation timeout (seconds)
VIDEO_TIMEOUT = int(os.getenv("DOLA_VIDEO_TIMEOUT", "300"))

# SQLite task database
DB_PATH = os.getenv("DOLA_DB_PATH", "tasks.db")

# Video download directory (videos are downloaded and stored here after generation; FastAPI serves them as static files)
DOWNLOAD_DIR = os.getenv("DOLA_DOWNLOAD_DIR", "downloads")

# Explicit browser proxy (P0 finding: cannot rely on the system proxy; changes to Clash rules route dola to a direct connection that gets blocked).
# Must be a proxy pointing to a JP/KR exit; empty = follow the system proxy (for debugging only).
PROXY = os.getenv("DOLA_PROXY", "http://127.0.0.1:7890")

# Whether the browser runs headless (login.py is always headed)
HEADLESS = os.getenv("DOLA_HEADLESS", "1") == "1"

# Base URL for the returned video URL (FastAPI static file service)
PUBLIC_BASE = os.getenv("DOLA_PUBLIC_BASE", f"http://127.0.0.1:{PORT}")

# Admin panel password (empty = panel has no auth, development mode)
ADMIN_KEY = os.getenv("DOLA_ADMIN_KEY", "")


# Dola 30-second/watermark-free Chromium extension (unpacked extension)
EXTENSION_DIR = os.getenv("DOLA_EXTENSION_DIR", "extensions/dola30")
EXTENSION_ENABLED = os.getenv("DOLA_EXTENSION_ENABLED", "1") == "1"

# Timezone for the Dola daily rate-limit reset; defaults to resetting at midnight Japan time.
LIMIT_RESET_TZ = os.getenv("DOLA_LIMIT_RESET_TZ", "Asia/Tokyo")

# Conservative minimum credits for the pre-generation balance check; the measured cost of Dola's current 2.5/30s is 2.
VIDEO_REQUIRED_POINTS = int(os.getenv("DOLA_VIDEO_REQUIRED_POINTS", "2"))


# Limits for downloading public reference images
REFERENCE_IMAGE_MAX_BYTES = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_BYTES", str(15 * 1024 * 1024)))
REFERENCE_DOWNLOAD_TIMEOUT = int(os.getenv("DOLA_REFERENCE_DOWNLOAD_TIMEOUT", "60"))
REFERENCE_IMAGE_MAX_COUNT = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_COUNT", "30"))

# Give tasks with reference images a longer asynchronous generation window for Dola (seconds).
REFERENCE_VIDEO_TIMEOUT = int(os.getenv("DOLA_REFERENCE_VIDEO_TIMEOUT", "900"))
