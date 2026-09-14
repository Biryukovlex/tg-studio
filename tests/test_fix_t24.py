from datetime import datetime, timezone

from app.db import Database


def _comment(db: Database, post_id: int, message_id: int) -> None:
    now = datetime.now(timezone.utc)
    db.upsert_comment(
        post_id=post_id,
        telegram_message_id=message_id,
        discussion_chat_id=9000,
        discussion_username="fixture-comments",
        sender_id=message_id,
        sender_name="Fixture commenter",
        sender_username="fixture",
        posted_at=now,
        edited_at=None,
        text="A fixture comment",
        media_type="",
        reactions=1,
        reply_to_message_id=None,
        sync_token=f"t24-{message_id}",
    )


def test_overview_queries_exclude_deactivated_channels(tmp_path):
    db = Database(tmp_path / "t24.sqlite")
    db.init_db()
    active = db.upsert_channel("@t24-active", "Active")
    retired = db.upsert_channel("@t24-retired", "Retired")
    now = datetime.now(timezone.utc)
    active_post = db.upsert_post(active, 1, now, "active post")
    retired_post = db.upsert_post(retired, 2, now, "retired post")
    db.add_snapshot_if_changed(active_post, 10, 2, 3, 4)
    db.add_snapshot_if_changed(retired_post, 100, 20, 30, 40)
    _comment(db, active_post, 11)
    _comment(db, retired_post, 22)

    with db.conn() as conn:
        conn.execute("UPDATE channels SET active=0 WHERE id=?", (retired,))

    kpis = db.kpis()
    assert kpis["posts"] == 1
    assert kpis["views"] == 10
    assert kpis["comments"] == 2
    assert kpis["collected_comments"] == 1
    assert len(db.latest_stats()) == 1
    assert len(db.all_comments()) == 1
    assert db.history_diagnostic()["total_posts"] == 1

    series = db.timeseries_totals(days=None)
    assert series["views"][-1] == 10
    assert series["posts_per_day"][-1] == 1

    # A specifically selected channel keeps the historical behaviour, even if
    # the channel is inactive and retained for archive inspection.
    assert db.kpis(retired)["posts"] == 1
    assert db.kpis(retired)["views"] == 100
    assert len(db.latest_stats(retired)) == 1
    assert len(db.all_comments(retired)) == 1
    assert db.history_diagnostic(retired)["total_posts"] == 1
