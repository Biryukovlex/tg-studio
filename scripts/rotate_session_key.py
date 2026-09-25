"""Re-encrypt stored Telegram connections from a previous key to the current key.

Operators rotate ``TELEGRAM_SESSION_ENCRYPTION_KEY`` by generating a fresh key
with ``scripts/generate_session_key.py``, setting the old value as
``TELEGRAM_SESSION_ENCRYPTION_KEY_PREVIOUS``, running this script, then
clearing the previous key once every row reports the current key version.

Usage:
    TELEGRAM_SESSION_ENCRYPTION_KEY=<new> \\
    TELEGRAM_SESSION_ENCRYPTION_KEY_PREVIOUS=<old> \\
        .venv/bin/python scripts/rotate_session_key.py

The report contains only counts and key-version fingerprints.  Secret material
is never printed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.session_crypto import SessionCipher  # noqa: E402


def rotate_connection_rows(
    rows: list[dict[str, Any]],
    current: SessionCipher,
    previous: SessionCipher,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Re-encrypt rows readable with the previous key under the current key.

    Returns (updates, report) where updates holds ``{id, encrypted_session,
    encrypted_api_hash, key_version}`` dicts ready to persist.  Rows already
    on the current key version are reported as skipped.
    """
    updates: list[dict[str, Any]] = []
    report = {"total": 0, "re_encrypted": 0, "skipped": 0, "unreadable": 0}
    for row in rows:
        report["total"] += 1
        row_id = row.get("id")
        try:
            try:
                session_string = current.decrypt(bytes(row["encrypted_session"]))
            except ValueError:
                session_string = previous.decrypt(bytes(row["encrypted_session"]))
        except (ValueError, TypeError, KeyError):
            report["unreadable"] += 1
            continue
        stored_hash = row.get("encrypted_api_hash")
        if stored_hash is not None:
            try:
                try:
                    api_hash = current.decrypt(bytes(stored_hash))
                except ValueError:
                    api_hash = previous.decrypt(bytes(stored_hash))
            except (ValueError, TypeError):
                report["unreadable"] += 1
                continue
        else:
            api_hash = str(row.get("api_hash") or "")
        if row.get("session_key_version") == current.key_version:
            # Both secrets must already use the current key before skipping.
            try:
                current.decrypt(bytes(row["encrypted_session"]))
                if stored_hash is not None:
                    current.decrypt(bytes(stored_hash))
            except ValueError:
                pass
            else:
                report["skipped"] += 1
                continue
        updates.append(
            {
                "id": row_id,
                "encrypted_session": current.encrypt(session_string),
                "encrypted_api_hash": current.encrypt(api_hash) if api_hash else None,
                "key_version": current.key_version,
            }
        )
        report["re_encrypted"] += 1
    return updates, report


async def _fetch_rows(database_url: str) -> tuple[Any, list[dict[str, Any]]]:
    from sqlalchemy import text

    from app.db_session import DatabaseSessionManager, normalize_database_url

    manager = DatabaseSessionManager(normalize_database_url(database_url))
    try:
        async with manager.session() as session:
            result = await session.execute(
                text(
                    """SELECT c.id, c.label, c.api_hash, c.encrypted_api_hash,
                              c.encrypted_session, c.session_key_version
                         FROM telegram_connections c
                        WHERE c.status='active' ORDER BY c.label"""
                )
            )
            rows = [dict(row) for row in result.mappings().all()]
            return manager, rows
    except BaseException:
        await manager.dispose()
        raise


async def _apply_updates(manager: Any, updates: list[dict[str, Any]]) -> None:
    from sqlalchemy import text

    async with manager.session() as session:
        for update in updates:
            await session.execute(
                text(
                    """UPDATE telegram_connections AS c
                          SET encrypted_session=:encrypted_session,
                              encrypted_api_hash=:encrypted_api_hash,
                              api_hash=NULL,
                              session_key_version=:key_version,
                              updated_at=now()
                        WHERE c.id=:id AND c.status='active'"""
                ),
                update,
            )
        await session.commit()


def main() -> int:
    current_key = os.environ.get("TELEGRAM_SESSION_ENCRYPTION_KEY", "")
    previous_key = os.environ.get("TELEGRAM_SESSION_ENCRYPTION_KEY_PREVIOUS", "")
    database_url = os.environ.get("DATABASE_URL", "")
    if not current_key or not previous_key or not database_url:
        print(
            "Set TELEGRAM_SESSION_ENCRYPTION_KEY, TELEGRAM_SESSION_ENCRYPTION_KEY_PREVIOUS "
            "and DATABASE_URL in the environment.",
            file=sys.stderr,
        )
        return 2
    current = SessionCipher(current_key)
    previous = SessionCipher(previous_key)

    async def run() -> dict[str, int]:
        manager, rows = await _fetch_rows(database_url)
        try:
            updates, report = rotate_connection_rows(rows, current, previous)
            if report["unreadable"]:
                raise RuntimeError(
                    f"rotation stopped: {report['unreadable']} connection row(s) could not be decrypted"
                )
            await _apply_updates(manager, updates)
            return report
        finally:
            await manager.dispose()

    report = asyncio.run(run())
    print(
        "rotation complete: "
        f"total={report['total']} re_encrypted={report['re_encrypted']} "
        f"skipped={report['skipped']} unreadable={report['unreadable']} "
        f"key_version={current.key_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
