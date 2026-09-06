from __future__ import annotations

from datetime import datetime, timezone

from app.studio.provenance import (
    build_research_bundle,
    cluster_stories,
    deduplicate_sources,
    rank_sources,
    select_sources,
    to_source_evidence,
)


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def source(url, title, excerpt, *, publisher="Publisher", published_at="2026-09-02T00:00:00+00:00", content_hash=""):
    return {
        "url": url,
        "title": title,
        "snippet": excerpt,
        "source_name": publisher,
        "published_at": published_at,
        "fetched_at": "2026-09-03T00:00:00+00:00",
        "source_hash": content_hash,
        "provider": "fixture",
        "accessible": True,
    }


def test_deduplication_uses_canonical_urls_hashes_and_headline_similarity():
    values = [
        source("https://Example.com/story?utm_source=x", "Market update", "Rates increase", content_hash="a"),
        source("https://example.com/story#read", "Different title", "Different excerpt", content_hash="b"),
        source("https://other.test/a", "Market update", "Rates increase", content_hash="c"),
    ]
    deduped = deduplicate_sources(values)
    assert len(deduped) == 2
    assert deduped[0].canonical_url == "https://example.com/story"


def test_clusters_group_duplicate_event_and_keep_primary_supporting_sources():
    values = [
        source("https://wire.test/a", "Central bank raises rates", "The central bank raised rates by 25 percent.", publisher="Wire", content_hash="a"),
        source("https://news.test/a", "Central bank raises rates", "Officials confirmed a 25 percent rate increase.", publisher="News", content_hash="b"),
        source("https://other.test/unrelated", "Football final tonight", "A different event is scheduled.", publisher="Other", content_hash="c"),
    ]
    stories = cluster_stories(values, query="central bank rates", topics=["central bank", "economy"], channel_evidence_ids=[1, 2], now=NOW)
    assert len(stories) == 2
    main = stories[0]
    assert len(main.source_ids) == 2
    assert main.primary_source_id
    assert main.supporting_source_ids
    assert main.channel_evidence_ids == (1, 2)
    assert main.topic_relevance > 0
    assert 0 <= main.score <= 1


def test_conflict_single_source_and_undated_flags_are_visible():
    values = [
        source("https://a.test/story", "Company confirms growth", "Revenue increased to 10 percent.", publisher="A", published_at=None),
        source("https://b.test/story", "Company denies growth", "Revenue decreased to 5 percent.", publisher="B", published_at="2026-09-02T00:00:00+00:00"),
    ]
    story = cluster_stories(values, query="company growth", now=NOW)[0]
    assert "possible_conflict" in story.conflict_flags
    assert "conflicting_numbers" in story.conflict_flags
    assert "undated_source" in story.conflict_flags
    assert "review" in " ".join(story.warnings).lower()

    single = cluster_stories([values[0]], query="company", now=NOW)[0]
    assert "single_source" in single.conflict_flags


def test_ranking_is_deterministic_and_selection_is_domain_diverse():
    values = [
        source("https://a.test/fresh", "Climate policy update", "Climate policy moves today.", publisher="A"),
        source("https://b.test/fresh", "Climate policy update", "Independent climate policy report.", publisher="B"),
        source("https://c.test/old", "Climate policy old", "Climate policy from years ago.", publisher="C", published_at="2020-01-01T00:00:00+00:00"),
    ]
    ranked_one = rank_sources(values, query="climate policy", topics=["climate"], now=NOW)
    ranked_two = rank_sources(values, query="climate policy", topics=["climate"], now=NOW)
    assert [item[0].source_id for item in ranked_one] == [item[0].source_id for item in ranked_two]
    chosen = select_sources(values, query="climate policy", topics=["climate"], max_sources=3, now=NOW)
    assert len(chosen) == 3
    assert len({item.domain for item in chosen}) == 3


def test_selection_does_not_let_one_publisher_fill_the_result_set():
    values = [
        source("https://farm.test/one", "Agent launch one", "First agent launch report."),
        source("https://farm.test/two", "Agent funding two", "Second agent funding report."),
        source("https://farm.test/three", "Agent benchmark three", "Third agent benchmark report."),
        source("https://official.test/release", "Official agent release", "Official release notes."),
        source("https://news.test/report", "Independent agent report", "Independent launch coverage."),
    ]

    chosen = select_sources(values, query="agent release", max_sources=4, now=NOW)

    assert len({item.domain for item in chosen[:3]}) == 3
    assert sum(item.domain == "farm.test" for item in chosen) <= 2


def test_source_roles_penalize_syndication_without_blocking_discovery():
    primary = to_source_evidence(
        source("https://github.com/example/agent/releases/tag/v1", "Agent release v1", "Open-source agent release notes.")
    )
    syndicated = to_source_evidence(
        source("https://finance.yahoo.com/news/example-agent-launch-120000.html", "Agent launch coverage", "Open-source agent launch copy.")
    )

    assert primary.metadata["source_role"] == "primary"
    assert syndicated.metadata["source_role"] == "syndication"
    assert primary.quality_score > syndicated.quality_score
    assert "not independent corroboration" in " ".join(syndicated.quality_notes)


def test_bundle_has_source_links_retrieval_times_and_safe_dump():
    bundle = build_research_bundle(
        [source("https://example.test/story", "A topic", "A factual summary.")],
        query="topic",
        topics=["topic"],
        channel_evidence_ids=[42],
        now=NOW,
    )
    dumped = bundle.model_dump(mode="json")
    assert dumped["stories"][0]["source_ids"]
    assert dumped["sources"][0]["url"].startswith("https://")
    assert dumped["sources"][0]["retrieved_at"]
    assert dumped["stories"][0]["channel_evidence_ids"] == [42]


def test_story_exposes_structured_ranking_quality_and_channel_links():
    bundle = build_research_bundle(
        [source("https://example.test/story", "Climate policy", "Climate policy update.")],
        query="climate",
        topics=["climate"],
        channel_evidence=[
            {"post_id": 8, "message_id": 80, "link": "https://t.me/example/80", "excerpt": "A winning climate post"}
        ],
        now=NOW,
    )
    story = bundle.stories[0]
    assert story.channel_evidence[0]["link"] == "https://t.me/example/80"
    assert story.score_breakdown["weights"]["topic_relevance"] == 0.4
    assert story.relevance_features["method"]
    assert story.novelty_features["method"]
    assert bundle.selected_source_ids == (bundle.sources[0].source_id,)
    assert bundle.sources[0].quality_score > 0


def test_optional_structured_model_assessment_is_bounded_and_explainable():
    def scorer(**_kwargs):
        return {
            "topic_relevance": 1.4,
            "novelty": -0.2,
            "matched_topics": ["climate"],
            "rationale": "Structured fixture assessment.",
        }

    bundle = build_research_bundle(
        [source("https://example.test/model", "Climate", "Climate report.")],
        query="climate",
        topics=["climate"],
        scorer=scorer,
        now=NOW,
    )
    story = bundle.stories[0]
    assert story.topic_relevance == 1.0
    assert story.novelty == 0.0
    assert story.score_breakdown["assessment_method"] == "model_assisted"
    assert story.relevance_features["rationale"] == "Structured fixture assessment."
