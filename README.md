# TG Studio

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Collects **views, reactions (likes), comments and shares (forwards)** from your
Telegram channel posts, archives the actual discussion comments under their
posts, and shows everything in a web admin panel + Telegram commands.

- Collector: Telethon **user session** (the only way Telegram exposes views/share counts)
- Current panel: FastAPI + PostgreSQL (with a read-only SQLite import/rollback archive),
  login-protected, charts via Chart.js
- Commands in Telegram: `/stats`, `/top`, `/last`, `/refresh` — send them to yourself in **Saved Messages**
- Community release is self-hostable with `.env` plus a persistent PostgreSQL volume;
  keep the legacy `data/` archive until migration reconciliation is complete.

## What is included

TG Studio is a self-hosted release candidate with a PostgreSQL runtime. It
combines:

- whole-history Telegram post and attributed-comment collection;
- engagement analytics and source-post links;
- a channel profile grounded in successful posts;
- an agent-centered writing Studio with durable conversations;
- optional private SearXNG research and SSRF-safe source reading;
- source-grounded Telegram-ready draft artifacts with copy and version history.

## Product tour

### Channel overview

![TG Studio channel overview with engagement totals and timeline](docs/screenshots/channel-overview.png)

### Post details and attributed comments

![Telegram post details with views, reactions, shares, and archived comments](docs/screenshots/post-comments.png)

### Per-post metric history

![Per-post engagement history and raw metric snapshots](docs/screenshots/post-metric-history.png)

### Agent-centered writing Studio

![Studio conversation, channel profile, source-grounded draft, and copy controls](docs/screenshots/studio-draft-workspace.png)

> The screenshots use local demonstration content. Draft claims shown in the
> interface are illustrative and should not be treated as factual announcements.

The architecture is intentionally compact:

```text
assistant-ui → FastAPI + PydanticAI → PostgreSQL
                         ↓
                 OpenRouter / SearXNG
```

The community release seeds one local workspace and keeps the administrator
experience simple. Workspace ownership is enforced across repositories,
channels, agent tools, jobs, and Studio records so a future SaaS edition does
not require a schema rewrite. Redis, Celery, billing, and team management are
not part of this release.

The legacy SQLite path is read-only and is used only for migration or rollback.
The importer is idempotent and reconciles row counts plus one-way hashes. Keep
the SQLite file as a rollback archive until the report is `all_match: true`.

The release is covered by automated tests, dependency audits, a production
Docker build, and continuous integration.

- [Deployment and release runbook](docs/deployment.md)
- [Privacy and data flow](docs/privacy.md)
- [License and notices](docs/license-notices.md)
- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)

Studio is always available. Configure the OpenRouter key only
on the server; it is never sent to the browser. Agent and research bounds are
fixed in `app/limits.py`. Studio routes require authentication and
PostgreSQL readiness.

---

## 1. Local setup (macOS/Linux)

```bash
cd "tg-studio"
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

### 1.1 Get API keys

Go to https://my.telegram.org → **API development tools** → create an app and
keep the resulting `api_id` and `api_hash` ready for the Settings page. In
PostgreSQL mode they do not need to be copied into `.env`.

### 1.2 Create session string (one-time login)

```bash
python scripts/generate_session.py
```

Choose QR login (recommended), then on your phone open **Telegram -> Settings
-> Devices -> Link Desktop Device** and scan the large QR opened locally in
your web browser.
Phone/code login is also available, but Telegram may deliver the code inside
an existing Telegram session rather than by SMS. Keep the printed
`SESSION_STRING` private; you will paste it into the authenticated Settings
page together with the API ID and hash.

### 1.3 Fill the rest of .env

| Key | Meaning |
|---|---|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | web panel login |
| `WEB_HOST` / `WEB_PORT` | web panel bind address and port |
| `SESSION_SECRET` | optional cookie secret (auto-generated if empty) |
| `DATABASE_URL` | PostgreSQL connection for the workspace store |
| `TELEGRAM_SESSION_ENCRYPTION_KEY` | key for encrypted session and secrets |
| `DATA_DIR` | filesystem location for secrets and import source |
| `STUDIO_SEARCH_BASE_URL` | private SearXNG endpoint when using web research |

> Channels, collection windows, provider keys, and research toggles are now configured at **/settings** in the web panel after login. The `.env` values for `CHANNELS`, `POLL_MINUTES`, `TRACK_DAYS`, `BACKFILL_LIMIT`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `STUDIO_SEARCH_ENABLED`, and `STUDIO_SEARCH_BLOCKED_DOMAINS` are optional first-start seeds; edit them in **/settings** afterwards.

> In PostgreSQL mode `API_ID`, `API_HASH`, and `SESSION_STRING` are optional
> fallbacks too. The application starts the authenticated web panel when no
> Telegram connection is available. Open **/settings**, save the Telegram
> connection and at least one channel, configure Studio/Research if needed,
> then restart the application once to start collection with those values.

> The bot account must be able to **read the channel** — your own account that
> owns/is subscribed to the channel already can.

### 1.4 Run

```bash
python -m app.main
```

- Web panel: http://127.0.0.1:8080 (login with ADMIN credentials)
- First-time PostgreSQL setup: http://127.0.0.1:8080/settings
- First cycle backfills the configured post history and linked discussion
  comments immediately, then polls every `POLL_MINUTES`.
- Stats are only written when values change, so the DB stays small.

### 1.5 PostgreSQL mode (M1)

Start PostgreSQL locally with Docker, copy the connection settings from
`.env.example`, and run the explicit migration service before starting the app:

```bash
docker compose up -d postgres
docker compose --profile migrate run --rm migrate
export DATABASE_URL=postgresql+asyncpg://tg_stats:change-me@127.0.0.1:55432/tg_stats
python scripts/migrate_sqlite_to_postgres.py \
  --sqlite data/stats.db --database-url "$DATABASE_URL"
```

Set `TELEGRAM_SESSION_ENCRYPTION_KEY` (generate one with
`python scripts/generate_session_key.py`) before using PostgreSQL session
persistence.  The key stays outside PostgreSQL and is never sent to the
browser.  Set `DATABASE_URL` in `.env` only after the reconciliation report is
successful; the application then uses PostgreSQL for collection, dashboard,
commands, exports, and Studio foundation records.

After switching to the PostgreSQL runtime, restore bodies that were truncated
by the pre-M1 SQLite release:

```bash
python scripts/restore_history.py       # safe counts/ranges only
python scripts/restore_history.py --run # Telegram refresh; reads all posts/comments and formatting
```

The refresh forces `TRACK_DAYS=0` and an unlimited Telegram iterator for that
run. It also refreshes Telegram entity metadata (bold, links, code, quotes,
spoilers, and similar rich-text marks) for each post. It is safe to repeat; the
importer/source archive is never modified by the refresh, and the summary
reports failed channels without exposing post or comment bodies.

### 1.6 Run the regression tests

Install the development dependencies and run the baseline suite without
connecting to Telegram:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

### 1.7 Inspect the SQLite archive before migration

Create a deterministic, read-only inventory of the current archive before any
database migration work:

```bash
python scripts/inventory_sqlite.py \\
  --db data/stats.db \\
  --output /tmp/tg-studio-inventory.json
```

The report contains schema metadata, row counts, timestamp ranges,
per-channel database IDs, integrity/constraint checks, and one-way row hashes.
It never exports post or comment bodies. Keep the generated report outside the
repository and do not commit it if it was created from a private archive.

### 1.8 Verify the PostgreSQL migration fixture

The M0 migration proof uses a disposable PostgreSQL 16 database and a
synthetic fixture. With `M0_POSTGRES_URL` pointing at that isolated database,
run:

```bash
M0_POSTGRES_URL=postgresql+asyncpg://user:password@127.0.0.1:5432/m0 \\
  python -m pytest tests/test_postgres_migration.py -q
```

The test runs `alembic upgrade head`, imports the fixture in dependency order,
and compares counts plus one-way row hashes. The probe schema is a feasibility
mirror; the final workspace-owned schema is delivered in M1.

### 1.9 Run the Content Studio (M5)

Complete the PostgreSQL setup, then start the app, sign in, and open
http://127.0.0.1:8080/studio. The first screen shows a setup state instead of
making provider calls when a required setting is missing.

In Studio, ask the agent in one free-text message to find a story and draft a
post. The artifact panel keeps the exact plain-text body editable, shows source
and channel evidence, autosaves with conflict recovery, and lets you copy the
final text for Telegram. Studio never publishes to Telegram.

On the first real Studio visit, review the concise OpenRouter disclosure and
choose **Allow and analyze**. Consent is bound to the provider/model/base URL
configuration; changing that configuration requires reviewing it again. The
first profile is prepared from local deterministic channel evidence and shows a
low-confidence warning when the channel has too little history. Discussion
comment bodies are never sent to the model.

For a local no-network protocol check only, set `STUDIO_TEST_MODE=true`.
This selects PydanticAI's deterministic TestModel;
it is an explicit test seam and must not be enabled for a hosted deployment.
With setup ready, create a conversation, send a free-text request, and the
assistant-ui shell streams the bounded agent response through the authenticated
FastAPI endpoint. Conversation messages, safe run-event summaries, and draft
versions are stored in PostgreSQL and reload from the workspace boundary. Ask
the agent to find a story and draft a post; the artifact panel keeps the exact
plain-text body, source chips, claim warnings, assumptions, and confidence
beside the chat. Direct edits autosave with optimistic locking, and **Copy for
Telegram** is disabled above 4,096 Unicode characters.

Build the production frontend bundle after changing `studio-frontend/`:

```bash
cd studio-frontend
NPM_CONFIG_CACHE=.npm-cache npm ci
npm run typecheck
npm run build
```

The old `/studio-spike` URL is retained only as a compatibility redirect and
API alias for existing bookmarks. It uses the production conversation/run
service and does not have a separate model, event log, or frontend bundle.
Run ownership, reload recovery, cancellation, and operator limits are enforced
by the PostgreSQL-backed Studio service and its automated test suite.

Studio agent and cancellation POSTs require the session-bound `X-CSRF-Token`
emitted on the protected page. OpenRouter credentials, Telegram sessions,
prompts, post/comment bodies, and raw provider payloads are never put in the
browser or ordinary run-event logs.

To make one provider request deliberately, set the backend-only key and run:

```bash
STUDIO_OPENROUTER_SMOKE=1 python scripts/smoke_openrouter.py
```

Without `STUDIO_OPENROUTER_SMOKE=1` the command exits as `skipped` and makes no
network request. The smoke output contains only status, model name, and output
length (never the key, prompt, or provider response).

## 2. Admin panel features

- KPI cards: total views / reactions / comments / shares (+ posts tracked)
- Cumulative chart over 7/14/30/90 days or all stored history, per-channel filter
- Posts table: sortable by any metric, Δ24h views, links to t.me
- Post detail page: full metric history, raw snapshots, and archived comments
- `↻ Refresh now`, post CSV export, and comment CSV export

## 3. Moving the community release to a server

The release is PostgreSQL-first. Follow [`docs/deployment.md`](docs/deployment.md)
for the complete Docker setup, explicit migration, SQLite reconciliation,
backup/restore, split-process topology, privacy, and troubleshooting runbook.
The abbreviated SSH-tunnel pattern below is useful after the database and
secrets have been prepared on the server.

### Option A — Docker (recommended)

```bash
# local: copy project
rsync -av --exclude .venv --exclude data ./ user@SERVER_IP:/opt/tg-studio/
rsync -av data user@SERVER_IP:/opt/tg-studio/   # keep collected history

# server:
ssh user@SERVER_IP
cd /opt/tg-studio
# edit .env: WEB_HOST=0.0.0.0 only behind a reverse proxy, otherwise keep 127.0.0.1
docker compose --profile migrate run --rm migrate
docker compose up -d --build
```

Panel is published on `127.0.0.1:8080` inside the server. Access options:

```bash
# simplest & safest: SSH tunnel from your machine, then open localhost:8080
ssh -L 8080:127.0.0.1:8080 user@SERVER_IP
```

Or put Caddy/nginx in front for HTTPS:

```
stats.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

### Option B — systemd + venv

```bash
sudo cp deploy/systemd/tg-studio.service /etc/systemd/system/   # fix User=/paths inside
sudo systemctl daemon-reload && sudo systemctl enable --now tg-studio
journalctl -u tg-studio -f
```

### Server hardening checklist

- UFW: allow `OpenSSH`; only expose 80/443 if you add a reverse proxy
- Keep `SESSION_STRING` private — it grants access to your Telegram account
- Back up PostgreSQL with the custom `pg_dump` command in `docs/deployment.md`;
  keep the SQLite file only as a read-only rollback archive.

## 4. Troubleshooting

| Symptom | Fix |
|---|---|
| `Session is not authorized` | re-run `scripts/generate_session.py`, update `.env` |
| Channel not resolved | public channels: use exact `@username`; private: use `-100...` id (get via @usernametobot → Chat ID) |
| Comments always 0 | channel has no **linked discussion group**, or post has no comments yet |
| Comment author says `Unknown / hidden` | Telegram did not expose an author for that discussion message |
| FloodWait in logs | normal under heavy load; collector auto-sleeps and continues next cycle |

## License

TG Studio is licensed under the [Apache License 2.0](LICENSE). See
[`NOTICE`](NOTICE) and the [third-party license notes](docs/license-notices.md)
for bundled asset attribution and the separate optional SearXNG service.
