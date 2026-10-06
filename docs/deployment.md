# Deployment and release runbook

This runbook describes the M7 community deployment and the process topology
used by a future hosted installation. PostgreSQL is the only runtime
database.

## Prerequisites

- Docker Desktop (or Docker Engine + Compose v2) for the recommended setup;
- a Telegram API ID/hash from [my.telegram.org](https://my.telegram.org);
- a Telegram user session that can read the configured channel;
- an administrator password;
- an OpenRouter key only when real Studio requests are enabled.

Do not put `.env`, `data/`, database dumps, Telegram session strings, API
hashes, or provider keys into Git. The repository's `.dockerignore` excludes
these paths from image builds.

## First local Docker setup

```bash
cd "tg-studio"
cp .env.example .env
```

Set the PostgreSQL password, `ADMIN_PASSWORD`, `DATABASE_URL`, and
`TELEGRAM_SESSION_ENCRYPTION_KEY`. Collect the Telegram API ID/hash and create
the session string, but keep them ready for the authenticated Settings page
instead of placing them in `.env`. The QR flow is the most reliable:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/generate_session.py
python scripts/generate_session_key.py
```

Keep the printed session string and Fernet key private. Put only the Fernet key
in `.env` as `TELEGRAM_SESSION_ENCRYPTION_KEY`; the session string is entered
after login at `/settings`. For a fresh install, replace the development
`POSTGRES_PASSWORD` with a strong value and keep the same value in the internal
`DATABASE_URL` if you set it explicitly:

```dotenv
POSTGRES_PASSWORD=<strong-local-or-server-password>
DATABASE_URL=postgresql+asyncpg://tg_stats:<same-password>@postgres:5432/tg_stats
```

The default Compose binding publishes PostgreSQL only on `127.0.0.1:55432` so
host-side migrations and backups can connect. Do not bind it to `0.0.0.0`.

Run the schema migration once, explicitly:

```bash
docker compose up -d postgres
docker compose --profile migrate run --rm migrate
```

Then start the community process:

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
```

The `all` role owns the Telegram session, scheduled collection, Telegram
commands, and the web panel. Open <http://127.0.0.1:8080>, sign in, and use
<http://127.0.0.1:8080/settings> to add the channel, Telegram API ID/hash and
session string, OpenRouter key/model, collection window, and research policy.
Restart `tg-studio` after saving the Telegram connection; subsequent starts
load the complete connection from PostgreSQL. Until it is configured, the
`all` role stays online as a web setup panel instead of exiting. Environment
Telegram values remain supported as first-start fallbacks.

## Studio and web research

To use the real provider, set:

```dotenv
OPENROUTER_API_KEY=<server-only-key>
OPENROUTER_MODEL=nex-agi/nex-n2.5-pro:free
```

On the first real request, the workspace owner must review and accept the
OpenRouter disclosure. The consent is tied to provider/model/base URL; changing
any of those values requires consent again. Keys are never returned to the
browser or ordinary run-event logs. The owner-only **Settings → Agent logs**
view contains complete tool outputs for local debugging, including any channel
text, web excerpts, and URLs returned by a tool.

Enable the optional private SearXNG profile for current stories:

```dotenv
STUDIO_SEARCH_ENABLED=true
STUDIO_SEARCH_BASE_URL=http://searxng:8080
```

```bash
docker compose --profile studio-search up -d searxng
docker compose up -d --build tg-studio
docker compose ps searxng
```

SearXNG has no host port and is not a `tg-studio` dependency. If it is stopped,
unreachable, or rate-limited, `/studio/api/research/health` reports degraded
research while collection, the dashboard, and local channel analysis continue.
Pin `SEARXNG_IMAGE` to a reviewed tag or digest before a hosted release and
review SearXNG's AGPL obligations separately.

## Explicit process roles

Community mode uses one process:

```dotenv
PROCESS_ROLE=all
```

For a hosted split, run the web and collector services instead of `tg-studio`:

```bash
docker compose --profile migrate run --rm migrate
docker compose --profile split up -d postgres tg-studio-web tg-studio-worker
```

`tg-studio-web` serves authenticated pages and Studio but never opens Telegram;
`tg-studio-worker` owns the user session, scheduled collection, and Telegram
commands. Both use the same PostgreSQL workspace boundary. The web refresh
button explains that refresh work belongs to the worker.

Do not start the default `tg-studio` service at the same time as the split
services on the same port. Scale the worker only after confirming Telegram
collection-job claims and PostgreSQL connection-pool limits for the deployment.

## Backups and restore

Back up PostgreSQL before migrations, upgrades, or changing provider/runtime
configuration. The custom dump is portable across PostgreSQL 16 installations:

```bash
mkdir -p backups
docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > "backups/tg-studio-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Restore only during a maintenance window. Stop application writers first and
verify the filename before running the command:

```bash
docker compose stop tg-studio tg-studio-web tg-studio-worker
docker compose exec -T postgres sh -c \
  'pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < backups/KNOWN_GOOD.dump
docker compose up -d
```

Never use `docker compose down -v` as a backup or reset operation: it deletes
the named PostgreSQL volume. Keep at least one encrypted/off-host copy of the
dump and restrict its filesystem permissions.

## Inspect or refresh stored history

The diagnostic reads PostgreSQL without creating or changing any rows. It
reports counts and date ranges, never post/comment bodies:

```bash
docker compose exec tg-studio python scripts/restore_history.py
```

To deliberately recollect complete Telegram history and formatting, run the
following command while the usual Telegram worker is stopped. This avoids two
processes using the same Telegram session. The refresh uses saved connection
settings, has no age/message-count limit, and updates PostgreSQL:

```bash
docker compose stop tg-studio
docker compose run --rm --no-deps tg-studio python scripts/restore_history.py --run
docker compose up -d tg-studio
```

For the split topology, stop/restart `tg-studio-worker` instead and use that
service for the one-shot command. Inspect the reported failed channels before
assuming the refresh completed.

## Frontend development and image rebuild

The Dockerfile builds the Studio frontend automatically. For local frontend
work:

```bash
cd studio-frontend
NPM_CONFIG_CACHE=.npm-cache npm ci
NPM_CONFIG_CACHE=.npm-cache npm run typecheck
NPM_CONFIG_CACHE=.npm-cache npm run build
```

The Python runtime uses `requirements.lock`, while `requirements.txt` remains
the documented range-based source for local development. Refresh the lockfile
deliberately when upgrading dependencies, run `pip check`, `pip-audit`, the
license check, and the full test suite, then rebuild the image.

## Troubleshooting

- `PostgreSQL schema is not migrated`: run `docker compose --profile migrate run --rm migrate`.
- `tg-studio` is unhealthy: inspect `docker compose logs tg-studio`; verify `/healthz`, `DATABASE_URL`, and the encryption key.
- `Session is not authorized`: regenerate the QR/session string; never paste it into logs or Git.
- Studio says `openrouter_key_missing`: set the server-side key and restart the app.
- Studio says research is degraded: check `docker compose ps searxng` and `GET /studio/api/research/health`; this does not indicate a collector failure.
- The split web refresh button reports worker ownership: this is expected; trigger `/refresh` from the all/worker deployment or restart the collector worker.
- Posts/comments appear short: run the history restoration command with `--run`; it forces whole-history collection and preserves Telegram formatting entities.
- A deployment must be stopped: use `docker compose down` only after taking a backup; never add `-v` unless intentionally destroying the database volume.

## Key and secret rotation

Rotate one secret at a time and keep a fresh PostgreSQL backup before each
step. Never print session strings, API hashes, or encryption keys to logs.

- **Telegram session** (account compromise or device loss): revoke the session
  in Telegram Settings → Devices, generate a new string with
  `python scripts/generate_session.py`, and save it on the Settings page.
- **OpenRouter key**: rotate it in the provider dashboard, update the stored
  key on the Settings page, and restart the app.
- **`SESSION_SECRET`** (cookie signing): changing it invalidates every login
  session; operators are signed out on the next request.
- **`TELEGRAM_SESSION_ENCRYPTION_KEY`**: generate a new key with
  `python scripts/generate_session_key.py`, set the old value as
  `TELEGRAM_SESSION_ENCRYPTION_KEY_PREVIOUS`, then re-encrypt every stored
  connection without printing secrets:

  ```bash
  TELEGRAM_SESSION_ENCRYPTION_KEY=<new> \
  TELEGRAM_SESSION_ENCRYPTION_KEY_PREVIOUS=<old> \
  DATABASE_URL=<url> \
      .venv/bin/python scripts/rotate_session_key.py
  ```

  Clear the previous key once the report shows zero `unreadable` rows. The
  collector also upgrades legacy rows transparently on read.
- **`data/.secret`** (local cookie fallback): include it in backups; losing it
  signs everyone out but does not lose collected data.

## Incident response

1. Revoke the affected credential at its source (Telegram Devices, provider
   dashboard) before touching the deployment.
2. Take a PostgreSQL backup for forensics, then rotate the secret using the
   procedure above.
3. Check `docker compose logs` for unexpected access and verify `/healthz`.
4. Do not put session strings, channel exports, or provider payloads in
   issues, pull requests, tests, or backups shared off-host.
