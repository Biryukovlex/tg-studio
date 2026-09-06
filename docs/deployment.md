# Deployment and release runbook

This runbook describes the M7 community deployment and the process topology
used by a future hosted installation. PostgreSQL is the runtime database of
record. The SQLite file is a read-only migration source or a rollback archive;
the application never dual-writes both stores.

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

Fill `API_ID`, `API_HASH`, `CHANNELS`, `ADMIN_PASSWORD`, and the Telegram
session. The QR flow is the most reliable:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/generate_session.py
python scripts/generate_session_key.py
```

Keep the printed session string and Fernet key private. Put them in `.env` as
`SESSION_STRING` and `TELEGRAM_SESSION_ENCRYPTION_KEY`. For a fresh install,
replace the development `POSTGRES_PASSWORD` with a strong value and keep the
same value in the internal `DATABASE_URL` if you set it explicitly:

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

The `all` role owns the Telegram session, scheduled collection, Telegram admin
commands, and the web panel. Open <http://127.0.0.1:8080>, sign in, and use
<http://127.0.0.1:8080/studio> when `STUDIO_ENABLED=true`.

## Studio and web research

Studio is disabled by default. To use the real provider, set:

```dotenv
STUDIO_ENABLED=true
STUDIO_TEST_MODE=false
OPENROUTER_API_KEY=<server-only-key>
OPENROUTER_MODEL=openai/gpt-4o-mini
```

On the first real request, the workspace owner must review and accept the
OpenRouter disclosure. The consent is tied to provider/model/base URL; changing
any of those values requires consent again. Keys are never returned to the
browser or ordinary logs.

Enable the optional private SearXNG profile for current stories:

```dotenv
STUDIO_SEARCH_ENABLED=true
STUDIO_SEARCH_PROVIDER=searxng
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

To disable Studio without changing analytics, set `STUDIO_ENABLED=false` and
recreate the app container:

```bash
docker compose up -d --build tg-studio
```

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

The legacy SQLite archive can be copied without modifying it:

```bash
docker compose stop tg-studio
cp -p data/stats.db "backups/stats-sqlite-$(date -u +%Y%m%dT%H%M%SZ).db"
docker compose up -d
```

## SQLite import and rollback rehearsal

Inventory the source first. The report contains counts, ranges, and one-way
hashes, never message/comment bodies:

```bash
.venv/bin/python scripts/inventory_sqlite.py \
  --db data/stats.db --output /tmp/tg-studio-inventory.json
```

With PostgreSQL migrated, run a dry-run import from a read-only source, then
the real import. Host-side commands use the localhost Compose port; inside the
`migrate` container use the internal hostname `postgres` instead:

```bash
export HOST_DATABASE_URL=postgresql+asyncpg://tg_stats:<password>@127.0.0.1:55432/tg_stats
DATABASE_URL="$HOST_DATABASE_URL" .venv/bin/python -m alembic upgrade head

.venv/bin/python scripts/migrate_sqlite_to_postgres.py \
  --sqlite data/stats.db --database-url "$HOST_DATABASE_URL" --dry-run

.venv/bin/python scripts/migrate_sqlite_to_postgres.py \
  --sqlite data/stats.db --database-url "$HOST_DATABASE_URL"
```

Proceed only when the JSON result says `all_match: true`, `source_read_only:
true`, and `source_untouched: true`. Keep the original SQLite file unchanged
until the PostgreSQL dashboard, comments, exports, commands, and Studio have
been verified. If the cutover fails before PostgreSQL becomes authoritative,
stop the new process and return to the untouched SQLite archive. After the
cutover, rollback is a PostgreSQL restore plus the previous image; do not
merge divergent SQLite and PostgreSQL writes.

After importing, restore complete Telegram bodies and rich formatting:

```bash
.venv/bin/python scripts/restore_history.py
.venv/bin/python scripts/restore_history.py --run
```

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
