"""Deterministic channel-performance intelligence for Studio.

The first intelligence layer deliberately has no model or network dependency.
It turns the latest known snapshot for each post into bounded, reproducible
evidence that a later profile agent can interpret.  Keeping this calculation
pure makes it safe to rerun, easy to audit, and useful when a channel has too
little history for confident conclusions.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field


ANALYTICS_VERSION = "m3.v1"
SCORING_WEIGHTS: dict[str, float] = {
    "view_score": 0.40,
    "reaction_score": 0.20,
    "comment_score": 0.20,
    "share_score": 0.20,
}
DEFAULT_MAX_EVIDENCE = 20
DEFAULT_TOP_MAX = 30
DEFAULT_BASELINE_MAX = 30
DEFAULT_RECENT_MAX = 20


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str) and value.strip():
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _compact_text(value: Any, limit: int = 600) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


class MetricBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    views: int = 0
    reactions: int = 0
    comments: int = 0
    shares: int = 0


class ScoreBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view_score: float = 0.0
    reaction_score: float = 0.0
    comment_score: float = 0.0
    share_score: float = 0.0
    traction_score: float = 0.0


class EvidencePost(BaseModel):
    """One bounded, source-addressable post used by an observation."""

    model_config = ConfigDict(extra="forbid")

    post_id: int
    message_id: int | None = None
    posted_at: datetime
    excerpt: str = Field(default="", max_length=600)
    link: str | None = None
    age_days: int = 0
    age_cohort: str
    metrics: MetricBundle
    scores: ScoreBundle
    style_eligible: bool = False


class EvidenceObservation(BaseModel):
    """A claim plus the exact evidence that supports it."""

    model_config = ConfigDict(extra="forbid")

    claim: str
    evidence_post_ids: list[int] = Field(default_factory=list, max_length=30)
    metrics: dict[str, float] = Field(default_factory=dict)
    uplift: float | None = None
    sample_size: int = 0
    confidence: str = "low"
    limitations: list[str] = Field(default_factory=list)


class ChannelAnalytics(BaseModel):
    """Versioned output of :func:`analyze_posts`."""

    model_config = ConfigDict(extra="forbid")

    analytics_version: str = ANALYTICS_VERSION
    channel_id: int
    analysis_at: datetime
    analysis_start: datetime | None = None
    analysis_end: datetime | None = None
    eligible_post_count: int = 0
    style_eligible_post_count: int = 0
    cohort_counts: dict[str, int] = Field(default_factory=dict)
    scoring_weights: dict[str, float] = Field(default_factory=lambda: dict(SCORING_WEIGHTS))
    top_posts: list[EvidencePost] = Field(default_factory=list)
    baseline_posts: list[EvidencePost] = Field(default_factory=list)
    recent_posts: list[EvidencePost] = Field(default_factory=list)
    evidence_posts: list[EvidencePost] = Field(default_factory=list)
    observations: list[EvidenceObservation] = Field(default_factory=list)
    low_data: bool = True
    confidence: str = "low"
    confidence_score: float = 0.25
    limitations: list[str] = Field(default_factory=list)
    input_hash: str


def _percentiles(values: list[float]) -> list[float]:
    """Return deterministic mid-rank percentiles in the inclusive [0, 1]."""

    if not values:
        return []
    if len(values) == 1:
        return [1.0]
    ordered = sorted(values)
    result: list[float] = []
    denominator = float(len(values) - 1)
    for value in values:
        lower = sum(1 for candidate in ordered if candidate < value)
        equal = sum(1 for candidate in ordered if candidate == value)
        result.append((lower + (equal - 1) / 2) / denominator)
    return result


def percentile_scores(values: Iterable[float]) -> list[float]:
    """Public wrapper used by tests and future analysis versions."""

    return _percentiles([float(value) for value in values])


def _cohort(age_days: int) -> str:
    if age_days <= 3:
        return "1-3d"
    if age_days <= 7:
        return "4-7d"
    if age_days <= 30:
        return "8-30d"
    if age_days <= 90:
        return "31-90d"
    return ">90d"


def _post_id(row: Mapping[str, Any]) -> int:
    for key in ("post_id", "id"):
        if row.get(key) is not None:
            return int(row[key])
    raise ValueError("analytics row is missing post_id")


def _normalise_row(row: Mapping[str, Any], *, now: datetime, channel_id: int) -> dict[str, Any] | None:
    if row.get("channel_id") is not None and int(row["channel_id"]) != int(channel_id):
        return None
    if bool(row.get("is_deleted", False)):
        return None
    posted_at = _parse_datetime(row.get("posted_at"))
    if posted_at is None:
        return None
    # A post is eligible only once it has a known snapshot.  ``snapshot_at``
    # and ``taken_at`` are accepted so repository and fixture rows share one
    # contract.  A post may explicitly set has_snapshot=False to fail closed.
    if row.get("has_snapshot") is False:
        return None
    if not any(key in row for key in ("snapshot_at", "taken_at", "views", "reactions", "comments", "shares")):
        return None
    snapshot_at = _parse_datetime(row.get("snapshot_at") or row.get("taken_at")) or now
    age = max(0, (now - posted_at).days)
    # By default very recent posts are excluded: their first 24 hours have
    # not had a comparable chance to accumulate traction.  Callers can pass
    # ``include_recent=True`` to inspect them explicitly.
    return {
        "post_id": _post_id(row),
        "message_id": int(row["message_id"]) if row.get("message_id") is not None else None,
        "posted_at": posted_at,
        "snapshot_at": snapshot_at,
        "text": row.get("text", "") or "",
        "views": _non_negative_int(row.get("views")),
        "reactions": _non_negative_int(row.get("reactions")),
        "comments": _non_negative_int(row.get("comments")),
        "shares": _non_negative_int(row.get("shares")),
        "age_days": age,
        "age_cohort": _cohort(age),
    }


def _canonical_input(rows: list[dict[str, Any]], channel_id: int, now: datetime) -> str:
    payload = {
        "analytics_version": ANALYTICS_VERSION,
        "channel_id": int(channel_id),
        "as_of": now.isoformat(),
        "rows": [
            {
                key: (value.isoformat() if isinstance(value, datetime) else value)
                for key, value in sorted(row.items())
                if key != "text"  # text is not part of scoring identity
            }
            for row in rows
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _link(identifier: str | None, message_id: int | None) -> str | None:
    if not identifier or message_id is None:
        return None
    handle = str(identifier).strip().lstrip("@").split("/")[-1]
    if not handle or handle.startswith("-"):
        return None
    return f"https://t.me/{handle}/{message_id}"


def _evidence(row: dict[str, Any], scores: ScoreBundle, identifier: str | None) -> EvidencePost:
    return EvidencePost(
        post_id=row["post_id"],
        message_id=row["message_id"],
        posted_at=row["posted_at"],
        excerpt=_compact_text(row["text"]),
        link=_link(identifier, row["message_id"]),
        age_days=row["age_days"],
        age_cohort=row["age_cohort"],
        metrics=MetricBundle(
            views=row["views"],
            reactions=row["reactions"],
            comments=row["comments"],
            shares=row["shares"],
        ),
        scores=scores,
        style_eligible=len("".join(str(row["text"]).split())) >= 80,
    )


def _mean(rows: list[EvidencePost], field: str) -> float:
    if not rows:
        return 0.0
    return sum(float(getattr(row.metrics, field)) for row in rows) / len(rows)


def analyze_posts(
    rows: Iterable[Mapping[str, Any]],
    channel_id: int,
    *,
    now: datetime | None = None,
    identifier: str | None = None,
    include_recent: bool = False,
    max_evidence_posts: int = DEFAULT_MAX_EVIDENCE,
) -> ChannelAnalytics:
    """Score a channel's latest post snapshots without making network calls.

    ``rows`` is intentionally mapping-shaped so it can consume PostgreSQL
    repository output or small synthetic fixtures.  Each row must include a
    post id, ``posted_at`` and at least one snapshot metric; comment bodies are
    not accepted or copied into the result.
    """

    as_of = _utc(now) or datetime.now(timezone.utc)
    normalised = [
        item
        for row in rows
        if (item := _normalise_row(row, now=as_of, channel_id=int(channel_id))) is not None
        and (include_recent or item["age_days"] >= 1)
    ]
    # A stable post id/date order is used everywhere ties can occur.
    normalised.sort(key=lambda item: (item["posted_at"], item["post_id"]))
    input_hash = _canonical_input(normalised, int(channel_id), as_of)
    n = len(normalised)
    metric_values = {
        "view_score": _percentiles([math.log1p(item["views"]) for item in normalised]),
        "reaction_score": _percentiles([item["reactions"] / max(item["views"], 1) for item in normalised]),
        "comment_score": _percentiles([item["comments"] / max(item["views"], 1) for item in normalised]),
        "share_score": _percentiles([item["shares"] / max(item["views"], 1) for item in normalised]),
    }
    evidence: list[EvidencePost] = []
    for index, item in enumerate(normalised):
        scores = ScoreBundle(
            view_score=metric_values["view_score"][index] if n else 0.0,
            reaction_score=metric_values["reaction_score"][index] if n else 0.0,
            comment_score=metric_values["comment_score"][index] if n else 0.0,
            share_score=metric_values["share_score"][index] if n else 0.0,
        )
        scores.traction_score = round(
            sum(getattr(scores, key) * weight for key, weight in SCORING_WEIGHTS.items()), 6
        )
        evidence.append(_evidence(item, scores, identifier))

    ranked = sorted(evidence, key=lambda post: (-post.scores.traction_score, -post.posted_at.timestamp(), -post.post_id))
    top_count = min(DEFAULT_TOP_MAX, max(5, math.ceil(n * 0.20))) if n else 0
    top = ranked[:top_count]
    baseline = [
        post
        for post in ranked
        if 0.40 <= post.scores.traction_score <= 0.60
    ][:DEFAULT_BASELINE_MAX]
    recent = sorted(evidence, key=lambda post: (post.posted_at, post.post_id), reverse=True)[:DEFAULT_RECENT_MAX]
    style_count = sum(post.style_eligible for post in evidence)
    low_data = n < 8
    confidence = "low" if low_data else ("medium" if n < 20 or style_count < 8 else "high")
    confidence_score = {"low": 0.25, "medium": 0.58, "high": 0.86}[confidence]
    limitations: list[str] = []
    if low_data:
        limitations.append(f"Only {n} eligible posts; treat topic and style patterns as directional.")
    if style_count < 8:
        limitations.append(f"Only {style_count} posts have at least 80 non-whitespace characters for style analysis.")
    if not include_recent:
        limitations.append("Posts younger than 24 hours are excluded from comparable traction scoring.")
    if not evidence:
        limitations.append("No post has a usable snapshot in the selected channel.")

    # A compact, fully evidence-linked baseline observation is useful to the
    # profile layer before it has a topic classifier.
    top_ids = [post.post_id for post in top[: max_evidence_posts]]
    baseline_ids = [post.post_id for post in baseline[: max_evidence_posts]]
    top_views = _mean(top, "views")
    baseline_views = _mean(baseline, "views")
    uplift = ((top_views / baseline_views) - 1.0) if baseline_views else None
    observations = [
        EvidenceObservation(
            claim="Highest-traction posts are the strongest available style/topic evidence.",
            evidence_post_ids=top_ids,
            metrics={"mean_views": round(top_views, 3), "mean_traction": round(_mean(top, "views") and sum(p.scores.traction_score for p in top) / len(top) if top else 0.0, 6)},
            uplift=round(uplift, 6) if uplift is not None else None,
            sample_size=len(top),
            confidence=confidence,
            limitations=list(limitations),
        ),
        EvidenceObservation(
            claim="The 40-60th-percentile posts provide the comparison baseline.",
            evidence_post_ids=baseline_ids,
            metrics={"mean_views": round(baseline_views, 3)},
            sample_size=len(baseline),
            confidence=confidence if baseline else "low",
            limitations=list(limitations) + (["No baseline posts fell in the comparison band."] if not baseline else []),
        ),
    ]
    return ChannelAnalytics(
        channel_id=int(channel_id),
        analysis_at=as_of,
        analysis_start=min((post.posted_at for post in evidence), default=None),
        analysis_end=max((post.posted_at for post in evidence), default=None),
        eligible_post_count=n,
        style_eligible_post_count=style_count,
        cohort_counts=dict(Counter(post.age_cohort for post in evidence)),
        top_posts=top[:max_evidence_posts],
        baseline_posts=baseline[:max_evidence_posts],
        recent_posts=recent[:DEFAULT_RECENT_MAX],
        evidence_posts=ranked[:max(1, min(int(max_evidence_posts), 100))] if ranked else [],
        observations=observations,
        low_data=low_data,
        confidence=confidence,
        confidence_score=confidence_score,
        limitations=limitations,
        input_hash=input_hash,
    )


# Names used by callers in early design notes; keep them as small aliases so
# the deterministic contract is discoverable without coupling the UI to one
# spelling.
build_channel_analytics = analyze_posts
analyze_channel_posts = analyze_posts


__all__ = [
    "ANALYTICS_VERSION",
    "SCORING_WEIGHTS",
    "ChannelAnalytics",
    "EvidenceObservation",
    "EvidencePost",
    "MetricBundle",
    "ScoreBundle",
    "analyze_posts",
    "analyze_channel_posts",
    "build_channel_analytics",
    "percentile_scores",
]
