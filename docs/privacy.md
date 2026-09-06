# Privacy and data flow

The application is self-hosted. The operator controls the database, Telegram
session, OpenRouter key, optional search service, logs, backups, and network
access. The defaults bind the web panel and PostgreSQL to localhost.

## Local storage

PostgreSQL stores workspace, channel, post, metric snapshot, attributed
discussion-comment, Studio conversation, run, source, research, profile, and
draft records. Telegram sessions are encrypted at rest with
`TELEGRAM_SESSION_ENCRYPTION_KEY`. The key is supplied outside the database and
is never returned through the web API. A legacy `data/stats.db` is used only as
a read-only migration source or rollback archive.

## OpenRouter disclosure

When real Studio mode is enabled, the server can send bounded channel evidence,
the user's instruction, short conversation context, and draft context to the
configured OpenRouter model. The user must grant workspace consent before the
first request, and consent becomes stale when provider/model/base URL changes.

Telegram API credentials, session strings, provider keys, raw provider payloads,
and private network topology are not sent to the browser or ordinary run-event
logs.

Discussion comment bodies are collected for the operator's archive and remain
available to local analytics/export routes, but are not included in the default
Studio model context. Comment counts may be used as an engagement signal.

## Web research

The optional SearXNG service is private to the Compose network and has no host
port. Search and source retrieval are bounded, normalized, cached, and
SSRF-checked. Source text is treated as untrusted data and cannot issue agent
instructions. If SearXNG or an upstream engine fails, research is marked
degraded and Telegram collection continues.

## Operator responsibilities

Use a strong administrator password and PostgreSQL password, protect `.env`
and backups, restrict reverse-proxy access, rotate provider/session keys using
the documented migration/recovery procedure, and honor Telegram/source-site
terms. Do not put private channel exports or real provider payloads in tests,
issues, or pull requests.
