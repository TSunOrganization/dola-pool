# dola-pool

dola.com (ByteDance Seedance video model) account pool → OpenAI-compatible video API distribution service.

## Structure

| File | Purpose |
|---|---|
| `dola_client.py` | Upstream SDK (protocol reverse engineering, from astrbot_plugin_doubao_free) |
| `pool.py` | Account pool management (cookie loading + multi-account round-robin + concurrency semaphore) |
| `store.py` | Task state persistence (SQLite) |
| `server.py` | FastAPI service, OpenAI-compatible interface |
| `config.py` | Configuration (environment variables) |
| `cookies.txt` | Pool cookies (one per line, gitignored) |
| `skills/skill.md` | Default skill attached to every prompt (skill system) |
| `tests/` | Standalone logic tests (skill attach + proxy rotation) |

## Deployment

```bash
# 1. Install dependencies (isolate with venv)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Prepare the account pool
cp cookies.txt.example cookies.txt
# Edit cookies.txt and fill in real cookies (one per line)

# 3. Configure the proxy (dola.com needs a JP/KR egress; mandatory if the server is in mainland China)
export HTTPS_PROXY=http://user:pass@jp-proxy:port   # Windows PowerShell: $env:HTTPS_PROXY=...
export HTTP_PROXY=$HTTPS_PROXY

# 4. Configure the service
export DOLA_API_KEYS=sk-xxx        # Public API key(s), comma-separated for multiple
export DOLA_MAX_CONCURRENCY=3      # Max concurrent tasks
export DOLA_VIDEO_TIMEOUT=300      # Video generation timeout (seconds)

# 5. Start
uvicorn server:app --host 0.0.0.0 --port 8000
```

## API usage

The externally exposed video durations are fixed at **10 seconds, 15 seconds, and 30 seconds**. The create-video endpoint uses 10 seconds by default; each database API Key can additionally have its own daily quota, concurrency cap, and allowed durations configured individually in the admin panel.

```bash
# Create a video task
curl -X POST http://127.0.0.1:8000/v1/videos/generations \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"seedance-v2.0","prompt":"a cat chasing a butterfly","size":"720x1280","duration":15}'
# -> {"id":"video_xxx","status":"queued","model":"seedance-v2.0","prompt":"..."}

# Query a task (status: queued -> processing -> completed / failed)
curl http://127.0.0.1:8000/v1/videos/video_xxx \
  -H "Authorization: Bearer sk-xxx"
# -> {"id":"video_xxx","status":"completed","video_url":"https://..."}

# Health check (number of available accounts in the pool)
curl http://127.0.0.1:8000/health
```

## Skill system (30s / Seedance 2.5, up to 10 videos per account)

Attach a `skill.md` together with the prompt to use Dola's skill path, which unlocks the 30-second duration, the Seedance 2.5 model and the larger per-account quota:

```bash
curl -X POST http://127.0.0.1:8000/v1/videos/generations \
  -H "Authorization: Bearer sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.5","prompt":"a drone shot over a snowy mountain lake","duration":30,
       "skill_markdown":"# my skill\n- cinematic, smooth camera motion"}'
```

- Drop your **ready-made `skill.md`** at `DOLA_SKILL_FILE` (default `skills/skill.md`). Every task then attaches that exact file through Dola's native file input (together with any reference images) and types the prompt — the same as attaching the file and pasting the prompt in the chat UI.
- `skill_markdown` is only an optional override: if a request sends it, that text is written to a temporary `skill.md` and attached instead.
- Set `DOLA_SKILL_ENABLED=0` to stop attaching the skill file.
- Run in skill mode with `DOLA_DAILY_LIMIT=10` and provide several exits in `DOLA_PROXIES`; when an account hits its daily limit the pool rotates to the next proxy (a new exit IP gives a fresh quota) instead of stopping.

## Notes

- Daily quota is configurable via `DOLA_DAILY_LIMIT` (default 2). With the Dola skill system an account can produce up to 10 videos per day — set `DOLA_DAILY_LIMIT=10`.
- The quota is tied to the egress IP. List several exits in `DOLA_PROXIES` (comma-separated) and the pool automatically rotates to the next proxy when an account hits its limit, giving it a fresh budget — the equivalent of changing the VPN server.
- API Keys support a daily task limit, a concurrent-generation limit, allowed durations, and an expiry time; a value of 0 means unlimited.
- Tasks record the API Key name, customer-level usage, and the actual account used; the server-side pending-task limit is controlled by `DOLA_MAX_PENDING_TASKS` (default 100).
- `video_url` is a temporary dola CDN link (expires in a few hours); for proper distribution you must download and re-host it to your own OSS/object storage inside `_run_task`.
- Cookies are valid for about 60 days; when they expire you must log in again and update `cookies.txt`.
- The domestic version doubao.com applies risk controls against Python HTTP clients, so this service only runs against the international dola.com.

## Required Dola extension (`extensions/dola30/`)

`30-second duration` and `watermark-free parsing` depend on a third-party Chromium extension (unpacked), which is distributed together with the repository.
Runtime behavior:

- `duration=30` requires this extension; when it is missing, `browser.py` raises `Dola extension directory does not exist`;
- Setting `DOLA_EXTENSION_ENABLED=0` disables the extension-loading path and runs only the 5/10-second durations natively supported by the web UI;
- The path can be overridden with `DOLA_EXTENSION_DIR` (default `extensions/dola30`);
- `dola-skill-pack-response.json` / `doubao-skill-pack-response.json` are the fixed data the extension needs in order to intercept and forge the skill-pack response; deleting them breaks duration unlocking.


## Disclaimer

This repository is a record of personal learning and interoperability research, and contains a reverse-engineered implementation of the upstream web interface; the code contains no accounts, cookies, keys, or usable credentials. `extensions/dola30/` is a third-party extension included with the repository at the buyer's discretion; its licensing and usage rights belong to the original author, and users must assess account and compliance risks themselves.
