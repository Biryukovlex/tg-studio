from datetime import datetime, timedelta, timezone

from app.studio.analytics import ANALYTICS_VERSION, analyze_posts, percentile_scores


AS_OF = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)


def _row(post_id: int, *, days: int, views: int, text: str | None = None, comments: int = 0, reactions: int = 0, shares: int = 0, **extra):
    return {
        "post_id": post_id,
        "message_id": post_id + 1000,
        "channel_id": 7,
        "posted_at": AS_OF - timedelta(days=days),
        "snapshot_at": AS_OF - timedelta(hours=1),
        "text": text if text is not None else ("A considered editorial post with enough words to qualify for style analysis. " * 2),
        "views": views,
        "comments": comments,
        "reactions": reactions,
        "shares": shares,
        **extra,
    }


def test_percentiles_are_deterministic_and_tie_safe():
    assert percentile_scores([1, 2, 3]) == [0.0, 0.5, 1.0]
    assert percentile_scores([4, 4, 4]) == [0.5, 0.5, 0.5]
    assert percentile_scores([1]) == [1.0]


def test_analytics_filters_deleted_wrong_channel_and_unseasoned_posts():
    rows = [
        _row(1, days=2, views=100),
        _row(2, days=0, views=1000),  # less than 24 hours old
        _row(3, days=2, views=500, is_deleted=True),
        _row(4, days=2, views=500, channel_id=99),
        _row(5, days=2, views=500, has_snapshot=False),
    ]
    result = analyze_posts(rows, 7, now=AS_OF, identifier="@example")
    assert [post.post_id for post in result.evidence_posts] == [1]
    assert result.evidence_posts[0].link == "https://t.me/example/1001"
    assert result.evidence_posts[0].style_eligible is True
    assert result.low_data is True


def test_traction_weights_and_groups_are_exposed_with_evidence():
    rows = [_row(index, days=(index % 5) + 1, views=index * 100, comments=index, reactions=index * 2, shares=index // 2) for index in range(1, 11)]
    result = analyze_posts(rows, 7, now=AS_OF, identifier="@example", max_evidence_posts=6)
    assert result.analytics_version == ANALYTICS_VERSION
    assert result.eligible_post_count == 10
    assert result.style_eligible_post_count == 10
    assert sum(result.cohort_counts.values()) == 10
    assert result.top_posts
    assert len(result.top_posts) <= 6
    assert all(post.post_id in {e.post_id for e in result.evidence_posts} for post in result.top_posts)
    assert result.observations[0].evidence_post_ids
    assert result.observations[0].sample_size == len(result.top_posts)
    assert result.input_hash


def test_low_data_confidence_and_input_hash_are_reproducible():
    rows = [_row(1, days=4, views=20), _row(2, days=31, views=50, text="short")]
    first = analyze_posts(rows, 7, now=AS_OF)
    second = analyze_posts(list(reversed(rows)), 7, now=AS_OF)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.confidence == "low"
    assert first.low_data is True
    assert any("eligible posts" in item for item in first.limitations)


def test_include_recent_is_an_explicit_opt_in():
    row = _row(1, days=0, views=10)
    assert analyze_posts([row], 7, now=AS_OF).eligible_post_count == 0
    assert analyze_posts([row], 7, now=AS_OF, include_recent=True).eligible_post_count == 1
