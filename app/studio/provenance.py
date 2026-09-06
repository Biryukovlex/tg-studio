"""Deterministic source provenance, story clustering, and ranking.

The LLM may interpret these records, but it does not decide source identity or
silently discard disagreements. Every visible story keeps real URLs, retrieval
times, source IDs, and concise warning flags.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .search import SearchResult, canonicalize_url
from .sources import SourceDocument

_TOKEN_RE = re.compile(r"[\w\u0400-\u04ff\u00c0-\u024f]{2,}", re.UNICODE)
_NUMBER_RE = re.compile(r"\b\d[\d.,:%-]*\b")
_STOPWORDS = {
    "about", "after", "again", "also", "because", "being", "between", "could", "from", "have", "into", "more", "most", "other", "over", "said", "some", "such", "than", "that", "their", "there", "these", "they", "this", "what", "when", "where", "which", "while", "with", "would", "your",
    "для", "если", "как", "когда", "который", "может", "над", "они", "после", "про", "сказал", "также", "того", "это", "этот", "что", "чтобы", "через", "или", "при", "был", "была", "были",
}
_CONTRADICTION_PAIRS = (
    ({"deny", "denies", "denied", "not", "no", "never", "отрицает", "нет", "не"}, {"confirm", "confirmed", "confirmed", "верно", "подтвердил"}),
    ({"increase", "increased", "рост", "вырос", "больше", "gain"}, {"decrease", "decreased", "падение", "упал", "меньше", "loss"}),
    ({"alive", "жив", "win", "won", "победа"}, {"dead", "умер", "lose", "lost", "поражение"}),
)

# These are source roles, not a universal publisher blacklist. Mirrors and
# press-release wires remain useful discovery leads, but they must not score
# like an original repository, paper, or independently edited article.
_PRIMARY_SOURCE_DOMAINS = {
    "arxiv.org",
    "github.com",
    "gitlab.com",
    "huggingface.co",
    "paperswithcode.com",
}
_SYNDICATION_DOMAINS = {
    "aol.com",
    "businesswire.com",
    "finance.yahoo.com",
    "globenewswire.com",
    "markets.businessinsider.com",
    "msn.com",
    "prnewswire.com",
}


def _matches_domain(domain: str, candidates: set[str]) -> bool:
    value = str(domain or "").lower().strip(".")
    return any(value == candidate or value.endswith(f".{candidate}") for candidate in candidates)


def _source_role(domain: str, url: str = "") -> str:
    if _matches_domain(domain, _PRIMARY_SOURCE_DOMAINS):
        return "primary"
    if _matches_domain(domain, _SYNDICATION_DOMAINS) or "/newswire/" in str(url or "").lower():
        return "syndication"
    return "editorial_or_official"


class StructuredTopicScorer(Protocol):
    """Optional server-side model assessment seam.

    Implementations return bounded ``topic_relevance``/``novelty`` values and
    a short rationale. The deterministic signals remain the fallback when a
    model is unavailable or returns an invalid structure.
    """

    def __call__(self, *, source: "SourceEvidence", topics: Sequence[str], query: str, recent_posts: Sequence[str]) -> Mapping[str, Any]: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(str(value or "")) if token.lower() not in _STOPWORDS}


def _similarity(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """Safe source metadata shared by search, reader, agent, and UI layers."""

    source_id: str
    url: str
    canonical_url: str
    title: str = ""
    publisher: str = ""
    domain: str = ""
    published_at: datetime | None = None
    retrieved_at: datetime = field(default_factory=_now)
    excerpt: str = ""
    content_hash: str = ""
    provider: str = ""
    query: str = ""
    accessible: bool = True
    status: str = "ok"
    warnings: tuple[str, ...] = ()
    injection_flags: tuple[str, ...] = ()
    quality_score: float = 0.0
    quality_notes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def undated(self) -> bool:
        return self.published_at is None

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "publisher": self.publisher,
            "domain": self.domain,
            "published_at": _iso(self.published_at),
            "retrieved_at": _iso(self.retrieved_at),
            "excerpt": self.excerpt,
            "content_hash": self.content_hash,
            "provider": self.provider,
            "query": self.query,
            "accessible": self.accessible,
            "status": self.status,
            "warnings": list(self.warnings),
            "injection_flags": list(self.injection_flags),
            "quality_score": round(self.quality_score, 6),
            "quality_notes": list(self.quality_notes),
            "metadata": dict(self.metadata),
        }

    as_dict = model_dump


@dataclass(frozen=True, slots=True)
class StoryCluster:
    cluster_id: str
    headline: str
    summary: str
    source_ids: tuple[str, ...]
    primary_source_id: str | None
    supporting_source_ids: tuple[str, ...]
    score: float
    topic_relevance: float
    freshness: float
    source_quality: float
    novelty: float
    status: str = "new"
    matched_topics: tuple[str, ...] = ()
    conflict_flags: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    channel_evidence_ids: tuple[int, ...] = ()
    published_at: datetime | None = None
    retrieved_at: datetime = field(default_factory=_now)
    # These fields make the ranking contract inspectable by the agent/UI. They
    # are structured inputs, not hidden model reasoning or an assertion of
    # universal source quality.
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    relevance_features: dict[str, Any] = field(default_factory=dict)
    novelty_features: dict[str, Any] = field(default_factory=dict)
    channel_evidence: tuple[dict[str, Any], ...] = ()

    @property
    def single_source(self) -> bool:
        return len(self.source_ids) <= 1

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "headline": self.headline,
            "summary": self.summary,
            "source_ids": list(self.source_ids),
            "primary_source_id": self.primary_source_id,
            "supporting_source_ids": list(self.supporting_source_ids),
            "score": round(self.score, 6),
            "topic_relevance": round(self.topic_relevance, 6),
            "freshness": round(self.freshness, 6),
            "source_quality": round(self.source_quality, 6),
            "novelty": round(self.novelty, 6),
            "status": self.status if self.status in {"new", "saved", "dismissed", "used"} else "new",
            "matched_topics": list(self.matched_topics),
            "conflict_flags": list(self.conflict_flags),
            "warnings": list(self.warnings),
            "channel_evidence_ids": list(self.channel_evidence_ids),
            "published_at": _iso(self.published_at),
            "retrieved_at": _iso(self.retrieved_at),
            "score_breakdown": dict(self.score_breakdown),
            "relevance_features": dict(self.relevance_features),
            "novelty_features": dict(self.novelty_features),
            "channel_evidence": [dict(item) for item in self.channel_evidence],
        }

    as_dict = model_dump


@dataclass(frozen=True, slots=True)
class ResearchBundle:
    query: str
    sources: tuple[SourceEvidence, ...] = ()
    stories: tuple[StoryCluster, ...] = ()
    warnings: tuple[str, ...] = ()
    retrieved_at: datetime = field(default_factory=_now)
    selected_source_ids: tuple[str, ...] = ()

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return {
            "query": self.query,
            "sources": [source.model_dump(mode=mode) for source in self.sources],
            "stories": [story.model_dump(mode=mode) for story in self.stories],
            "warnings": list(self.warnings),
            "retrieved_at": _iso(self.retrieved_at),
            "selected_source_ids": list(self.selected_source_ids),
        }

    as_dict = model_dump


def _source(value: SourceEvidence | SearchResult | SourceDocument | dict[str, Any]) -> SourceEvidence:
    if isinstance(value, SourceEvidence):
        # Reclassify hydrated rows so deployments pick up source-policy
        # improvements without requiring a destructive research-data rewrite.
        metadata = dict(value.metadata)
        metadata["source_role"] = _source_role(value.domain, value.canonical_url)
        return replace(
            value,
            quality_score=_quality(value),
            quality_notes=tuple(dict.fromkeys([*value.quality_notes, *_quality_notes(value)])),
            metadata=metadata,
        )
    if isinstance(value, SearchResult):
        data = value.model_dump()
    elif isinstance(value, SourceDocument):
        data = value.model_dump()
    elif isinstance(value, dict):
        data = dict(value)
    else:
        raise TypeError("source must be a search result, source document, evidence, or mapping")
    url = str(data.get("url") or data.get("final_url") or data.get("canonical_url") or "").strip()
    canonical = canonicalize_url(str(data.get("canonical_url") or data.get("final_url") or url))
    domain = str(data.get("domain") or (urlsplit(canonical).hostname if canonical else "") or "").lower()
    title = str(data.get("title") or "").strip()
    excerpt = str(data.get("excerpt") or data.get("snippet") or data.get("text") or "").strip()[:2_000]
    source_hash = str(data.get("content_hash") or data.get("source_hash") or "")
    source_id = str(data.get("source_id") or "").strip()
    if not source_id:
        source_id = hashlib.sha256(f"{canonical}:{source_hash or title}".encode("utf-8")).hexdigest()[:24]
    retrieved = _aware(data.get("retrieved_at") or data.get("fetched_at")) or _now()
    published = _aware(data.get("published_at"))
    publisher = str(data.get("publisher") or data.get("source_name") or data.get("source") or domain).strip()[:160]
    warnings = tuple(str(item) for item in (data.get("warnings") or ()) if str(item))
    flags = tuple(str(item) for item in (data.get("injection_flags") or ()) if str(item))
    status = str(data.get("status") or ("ok" if data.get("accessible", True) else "inaccessible"))
    role = _source_role(domain, canonical)
    item = SourceEvidence(
        source_id=source_id,
        url=url,
        canonical_url=canonical,
        title=title[:500],
        publisher=publisher,
        domain=domain,
        published_at=published,
        retrieved_at=retrieved,
        excerpt=excerpt,
        content_hash=source_hash,
        provider=str(data.get("provider") or ""),
        query=str(data.get("query") or ""),
        accessible=bool(data.get("accessible", status == "ok")),
        status=status,
        warnings=warnings,
        injection_flags=flags,
        metadata={"provenance": dict(data.get("provenance") or {}), "source_role": role},
    )
    # Quality is deliberately a bounded, transparent heuristic.  It is
    # persisted with the source so a later model can explain the limitation
    # instead of treating this score as an objective publisher ranking.
    return replace(item, quality_score=_quality(item), quality_notes=_quality_notes(item))


def to_source_evidence(value: SourceEvidence | SearchResult | SourceDocument | dict[str, Any]) -> SourceEvidence:
    return _source(value)


def deduplicate_sources(values: Iterable[SourceEvidence | SearchResult | SourceDocument | dict[str, Any]], *, similarity_threshold: float = 0.86) -> list[SourceEvidence]:
    """Deduplicate URL, content-hash, publisher/headline, and near-copy rows."""

    result: list[SourceEvidence] = []
    by_url: dict[str, int] = {}
    by_hash: dict[str, int] = {}
    for value in values:
        item = _source(value)
        duplicate_index: int | None = by_url.get(item.canonical_url) if item.canonical_url else None
        if duplicate_index is None and item.content_hash:
            duplicate_index = by_hash.get(item.content_hash)
        if duplicate_index is None:
            for index, existing in enumerate(result):
                if item.publisher and existing.publisher and item.title and existing.title and item.publisher.lower() == existing.publisher.lower() and _similarity(item.title, existing.title) >= similarity_threshold:
                    duplicate_index = index
                    break
                if item.title and existing.title and _similarity(f"{item.title} {item.excerpt}", f"{existing.title} {existing.excerpt}") >= similarity_threshold:
                    duplicate_index = index
                    break
        if duplicate_index is None:
            result.append(item)
            index = len(result) - 1
            if item.canonical_url:
                by_url[item.canonical_url] = index
            if item.content_hash:
                by_hash[item.content_hash] = index
            continue
        # Prefer the accessible/dated/longer record while keeping the stable
        # URL identity. This does not merge article text into storage.
        existing = result[duplicate_index]
        preferred = item if (item.accessible, item.published_at is not None, len(item.excerpt)) > (existing.accessible, existing.published_at is not None, len(existing.excerpt)) else existing
        result[duplicate_index] = preferred
    return result


def _freshness(source: SourceEvidence, *, now: datetime, recency_days: int | None = None) -> float:
    if source.published_at is None:
        return 0.25
    if source.published_at > now:
        return 0.0
    age = max(0.0, (now - source.published_at).total_seconds() / 86_400)
    if recency_days is not None and age > max(1, recency_days):
        return max(0.05, math.exp(-age / max(1.0, recency_days)))
    return max(0.05, min(1.0, math.exp(-age / 30.0)))


def _quality(source: SourceEvidence) -> float:
    score = 0.0
    if source.canonical_url.startswith("https://"):
        score += 0.30
    elif source.canonical_url.startswith("http://"):
        score += 0.15
    if source.domain and not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", source.domain):
        score += 0.20
    if source.title:
        score += 0.20
    if source.excerpt:
        score += 0.15
    if source.published_at is not None:
        score += 0.10
    role = _source_role(source.domain, source.canonical_url)
    if role == "primary":
        score += 0.05
    elif role == "syndication":
        score -= 0.35
    return max(0.0, min(1.0, score))


def _quality_notes(source: SourceEvidence) -> tuple[str, ...]:
    notes: list[str] = []
    if source.canonical_url.startswith("http://"):
        notes.append("Source uses HTTP rather than HTTPS.")
    if not source.title:
        notes.append("Source did not provide a headline.")
    if not source.excerpt:
        notes.append("No bounded excerpt was available.")
    if source.published_at is None:
        notes.append("Publication date is unavailable.")
    if not source.accessible or source.status != "ok":
        notes.append("Source could not be fully accessed.")
    if source.injection_flags:
        notes.append("Page contained instruction-like text; it was treated as untrusted data.")
    role = _source_role(source.domain, source.canonical_url)
    if role == "primary":
        notes.append("Repository, model hub, or paper source; suitable for primary verification.")
    elif role == "syndication":
        notes.append("Syndicated or press-release copy; use as a discovery lead, not independent corroboration.")
    return tuple(dict.fromkeys(notes))


def _channel_evidence(values: Sequence[Mapping[str, Any]] | Sequence[dict[str, Any]] = ()) -> tuple[dict[str, Any], ...]:
    """Keep only bounded, linkable channel evidence fields in a story record."""

    safe: list[dict[str, Any]] = []
    for value in values or ():
        if not isinstance(value, Mapping):
            continue
        try:
            post_id = int(value.get("post_id") or value.get("id"))
        except (TypeError, ValueError):
            continue
        record: dict[str, Any] = {"post_id": post_id}
        for key in ("message_id", "posted_at", "age_days", "age_cohort", "link", "excerpt", "metrics", "scores"):
            item = value.get(key)
            if item is None:
                continue
            if key in {"excerpt"}:
                record[key] = " ".join(str(item).split())[:600]
            elif key in {"metrics", "scores"} and isinstance(item, Mapping):
                record[key] = {str(name): item[name] for name in item if str(name) in {"views", "reactions", "comments", "shares", "traction_score", "view_score", "reaction_score", "comment_score", "share_score"}}
            elif key == "link":
                link = str(item).strip()
                if link.startswith("https://t.me/"):
                    record[key] = link[:300]
            elif key == "posted_at":
                parsed = _aware(item)
                if parsed:
                    record[key] = _iso(parsed)
            else:
                record[key] = item
        safe.append(record)
        if len(safe) >= 20:
            break
    return tuple(safe)


def _relevance_features(source: SourceEvidence, topics: Sequence[str], query: str, matched: Sequence[str]) -> dict[str, Any]:
    vocabulary = sorted(_tokens(" ".join(topics)) | _tokens(query))
    return {
        "method": "token_overlap_v1",
        "assessment_method": "deterministic_fallback",
        "matched_terms": list(sorted(set(matched)))[:20],
        "matched_term_count": len(set(matched)),
        "vocabulary_size": len(vocabulary),
        "topic_count": len([topic for topic in topics if str(topic).strip()]),
        "query_present": bool(str(query).strip()),
        "model_input": "Use these bounded features as evidence; do not treat the heuristic as universal truth.",
    }


def _novelty_features(source: SourceEvidence, recent_posts: Sequence[str]) -> dict[str, Any]:
    text = f"{source.title} {source.excerpt}"
    similarities = [_similarity(text, post) for post in recent_posts if str(post).strip()]
    maximum = max(similarities, default=0.0)
    return {
        "method": "recent_post_jaccard_v1",
        "assessment_method": "deterministic_fallback",
        "recent_post_count": len(similarities),
        "max_recent_similarity": round(maximum, 6),
        "model_input": "Use this bounded similarity signal when explaining novelty; it is not a semantic guarantee.",
    }


def _structured_assessment(
    source: SourceEvidence,
    *,
    topics: Sequence[str],
    query: str,
    recent_posts: Sequence[str],
    scorer: StructuredTopicScorer | Callable[..., Mapping[str, Any]] | None,
) -> tuple[float, tuple[str, ...], float, dict[str, Any], dict[str, Any]]:
    relevance, matched = _relevance(source, topics, query)
    novelty = _novelty(source, recent_posts)
    relevance_features = _relevance_features(source, topics, query, matched)
    novelty_features = _novelty_features(source, recent_posts)
    if scorer is None:
        return relevance, matched, novelty, relevance_features, novelty_features
    try:
        candidate = scorer(source=source, topics=topics, query=query, recent_posts=recent_posts)
    except Exception:  # noqa: BLE001 - invalid optional model output falls back safely
        return relevance, matched, novelty, relevance_features, novelty_features
    if not isinstance(candidate, Mapping):
        return relevance, matched, novelty, relevance_features, novelty_features
    try:
        model_relevance = max(0.0, min(1.0, float(candidate.get("topic_relevance", relevance))))
        model_novelty = max(0.0, min(1.0, float(candidate.get("novelty", novelty))))
    except (TypeError, ValueError):
        return relevance, matched, novelty, relevance_features, novelty_features
    model_topics = tuple(sorted({str(item).strip()[:120] for item in (candidate.get("matched_topics") or matched) if str(item).strip()}))[:20]
    rationale = " ".join(str(candidate.get("rationale") or "").split())[:500]
    for features, score, name in (
        (relevance_features, model_relevance, "topic_relevance"),
        (novelty_features, model_novelty, "novelty"),
    ):
        features.update({"assessment_method": "model_assisted", "model_score": round(score, 6)})
        if rationale:
            features["rationale"] = rationale
        features["score_name"] = name
    return model_relevance, model_topics, model_novelty, relevance_features, novelty_features


def _relevance(source: SourceEvidence, topics: Sequence[str], query: str) -> tuple[float, tuple[str, ...]]:
    vocabulary = _tokens(" ".join(topics)) | _tokens(query)
    source_tokens = _tokens(f"{source.title} {source.excerpt}")
    if not vocabulary or not source_tokens:
        return 0.0, ()
    overlap = vocabulary & source_tokens
    return min(1.0, len(overlap) / max(1, min(len(vocabulary), 8))), tuple(sorted(overlap))


def _novelty(source: SourceEvidence, recent_posts: Sequence[str]) -> float:
    if not recent_posts:
        return 0.5
    text = f"{source.title} {source.excerpt}"
    similarity = max((_similarity(text, post) for post in recent_posts), default=0.0)
    return max(0.0, min(1.0, 1.0 - similarity))


def _conflicts(sources: Sequence[SourceEvidence]) -> tuple[str, ...]:
    flags: list[str] = []
    if len(sources) == 1:
        flags.append("single_source")
    if any(source.published_at is None for source in sources):
        flags.append("undated_source")
    if any(not source.accessible or source.status != "ok" for source in sources):
        flags.append("inaccessible_source")
    number_sets = {tuple(_NUMBER_RE.findall(f"{source.title} {source.excerpt}")) for source in sources if _NUMBER_RE.findall(f"{source.title} {source.excerpt}")}
    if len(number_sets) > 1:
        flags.append("conflicting_numbers")
    token_sets = [_tokens(f"{source.title} {source.excerpt}") for source in sources]
    for left, right in _CONTRADICTION_PAIRS:
        has_left = any(tokens & left for tokens in token_sets)
        has_right = any(tokens & right for tokens in token_sets)
        if has_left and has_right:
            flags.append("possible_conflict")
            break
    return tuple(dict.fromkeys(flags))


def _cluster_id(sources: Sequence[SourceEvidence]) -> str:
    key = "|".join(sorted(source.canonical_url or source.source_id for source in sources))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def cluster_stories(
    values: Iterable[SourceEvidence | SearchResult | SourceDocument | dict[str, Any]],
    *,
    query: str = "",
    topics: Sequence[str] = (),
    recent_posts: Sequence[str] = (),
    channel_evidence_ids: Sequence[int] = (),
    channel_evidence: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
    recency_days: int | None = None,
    similarity_threshold: float = 0.42,
    max_supporting_sources: int = 4,
    scorer: StructuredTopicScorer | Callable[..., Mapping[str, Any]] | None = None,
) -> list[StoryCluster]:
    """Group similar coverage of an event and rank clusters deterministically."""

    moment = now.astimezone(timezone.utc) if now and now.tzinfo else (now.replace(tzinfo=timezone.utc) if now else _now())
    sources = deduplicate_sources(values)
    evidence_records = _channel_evidence(channel_evidence)
    evidence_ids = tuple(dict.fromkeys(
        [int(item) for item in channel_evidence_ids]
        + [int(item["post_id"]) for item in evidence_records if item.get("post_id") is not None]
    ))[:20]
    groups: list[list[SourceEvidence]] = []
    for source in sources:
        haystack = f"{source.title} {source.excerpt}"
        group = next((candidate for candidate in groups if _similarity(haystack, f"{candidate[0].title} {candidate[0].excerpt}") >= similarity_threshold), None)
        if group is None:
            groups.append([source])
        else:
            group.append(source)
    clusters: list[StoryCluster] = []
    for group in groups:
        scored: list[tuple[float, SourceEvidence, float, float, float, float, tuple[str, ...], dict[str, Any], dict[str, Any]]] = []
        for source in group:
            relevance, matched, novelty, relevance_features, novelty_features = _structured_assessment(
                source,
                topics=topics,
                query=query,
                recent_posts=recent_posts,
                scorer=scorer,
            )
            fresh = _freshness(source, now=moment, recency_days=recency_days)
            quality = _quality(source)
            score = 0.40 * relevance + 0.20 * fresh + 0.20 * quality + 0.20 * novelty
            scored.append((score, source, relevance, fresh, quality, novelty, matched, relevance_features, novelty_features))
        scored.sort(key=lambda item: (-item[0], item[1].published_at is None, item[1].domain, item[1].source_id))
        primary = scored[0][1]
        support = [item[1] for item in scored[1:] if item[1].domain != primary.domain][:max(0, max_supporting_sources)]
        selected = [primary, *support]
        flags = _conflicts(selected)
        warnings = tuple(
            {
                "single_source": "Only one accessible source covers this story.",
                "undated_source": "At least one source has no publication date.",
                "inaccessible_source": "At least one candidate source was inaccessible.",
                "conflicting_numbers": "Sources report different numeric details; review before drafting.",
                "possible_conflict": "Sources may disagree; the agent must state the uncertainty.",
            }[flag]
            for flag in flags
            if flag in {"single_source", "undated_source", "inaccessible_source", "conflicting_numbers", "possible_conflict"}
        )
        relevance = max(item[2] for item in scored)
        freshness = max(item[3] for item in scored)
        quality = max(item[4] for item in scored)
        novelty = max(item[5] for item in scored)
        headline = primary.title or primary.domain or "Untitled story"
        summary = primary.excerpt or headline
        cluster_warnings = list(warnings)
        if not evidence_ids:
            cluster_warnings.append("No channel performance evidence was available to validate this fit.")
        elif not any(item.get("link") for item in evidence_records):
            cluster_warnings.append("Channel evidence has no public Telegram link.")
        primary_assessment = next(item for item in scored if item[1].source_id == primary.source_id)
        clusters.append(
            StoryCluster(
                cluster_id=_cluster_id(group),
                headline=headline,
                summary=summary[:2_000],
                source_ids=tuple(source.source_id for source in selected),
                primary_source_id=primary.source_id,
                supporting_source_ids=tuple(source.source_id for source in support),
                score=0.40 * relevance + 0.20 * freshness + 0.20 * quality + 0.20 * novelty,
                topic_relevance=relevance,
                freshness=freshness,
                source_quality=quality,
                novelty=novelty,
                status="new",
                matched_topics=tuple(sorted(set().union(*(set(item[6]) for item in scored)))),
                conflict_flags=flags,
                warnings=tuple(dict.fromkeys(cluster_warnings)),
                channel_evidence_ids=evidence_ids,
                published_at=primary.published_at,
                retrieved_at=max((source.retrieved_at for source in selected), default=moment),
                score_breakdown={
                    "formula": "0.40*topic_relevance + 0.20*freshness + 0.20*source_quality + 0.20*novelty",
                    "weights": {"topic_relevance": 0.40, "freshness": 0.20, "source_quality": 0.20, "novelty": 0.20},
                    "components": {
                        "topic_relevance": round(relevance, 6),
                        "freshness": round(freshness, 6),
                        "source_quality": round(quality, 6),
                        "novelty": round(novelty, 6),
                    },
                        "assessment_method": primary_assessment[7].get("assessment_method", "deterministic_fallback"),
                        "limitations": ["Heuristic signals are model-facing evidence, not a universal source ranking."],
                    },
                relevance_features=dict(primary_assessment[7]),
                novelty_features=dict(primary_assessment[8]),
                channel_evidence=evidence_records,
            )
        )
    clusters.sort(key=lambda item: (-item.score, item.published_at is None, item.cluster_id))
    return clusters


def rank_sources(
    values: Iterable[SourceEvidence | SearchResult | SourceDocument | dict[str, Any]],
    *,
    query: str = "",
    topics: Sequence[str] = (),
    recent_posts: Sequence[str] = (),
    now: datetime | None = None,
    recency_days: int | None = None,
    scorer: StructuredTopicScorer | Callable[..., Mapping[str, Any]] | None = None,
) -> list[tuple[SourceEvidence, float]]:
    """Return individual sources using the same explainable score weights."""

    moment = _aware(now) or _now()
    scored = []
    for source in deduplicate_sources(values):
        relevance, _matched, novelty, _relevance_features, _novelty_features = _structured_assessment(
            source,
            topics=topics,
            query=query,
            recent_posts=recent_posts,
            scorer=scorer,
        )
        freshness = _freshness(source, now=moment, recency_days=recency_days)
        quality = _quality(source)
        score = 0.40 * relevance + 0.20 * freshness + 0.20 * quality + 0.20 * novelty
        scored.append((source, score))
    scored.sort(key=lambda item: (-item[1], item[0].published_at is None, item[0].source_id))
    return scored


def select_sources(
    values: Iterable[SourceEvidence | SearchResult | SourceDocument | dict[str, Any]],
    *,
    query: str = "",
    topics: Sequence[str] = (),
    recent_posts: Sequence[str] = (),
    max_sources: int = 6,
    now: datetime | None = None,
    recency_days: int | None = None,
    scorer: StructuredTopicScorer | Callable[..., Mapping[str, Any]] | None = None,
) -> list[SourceEvidence]:
    """Select a bounded, domain-diverse source set for a story or draft."""

    ranked = rank_sources(
        values,
        query=query,
        topics=topics,
        recent_posts=recent_posts,
        now=now,
        recency_days=recency_days,
        scorer=scorer,
    )
    chosen: list[SourceEvidence] = []
    limit = max(1, min(int(max_sources), 12))
    # First take the strongest result from every publisher, then (only if
    # needed) a second result. A content farm cannot occupy the whole answer.
    for per_domain_limit in (1, 2):
        domain_counts: dict[str, int] = {}
        for existing in chosen:
            domain_counts[existing.domain] = domain_counts.get(existing.domain, 0) + 1
        for source, _score in ranked:
            if len(chosen) >= limit:
                break
            if source in chosen or domain_counts.get(source.domain, 0) >= per_domain_limit:
                continue
            chosen.append(source)
            domain_counts[source.domain] = domain_counts.get(source.domain, 0) + 1
        if len(chosen) >= limit:
            break
    if not chosen:
        return []
    return chosen


def build_research_bundle(
    values: Iterable[SourceEvidence | SearchResult | SourceDocument | dict[str, Any]],
    *,
    query: str = "",
    topics: Sequence[str] = (),
    recent_posts: Sequence[str] = (),
    channel_evidence_ids: Sequence[int] = (),
    channel_evidence: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
    max_selected_sources: int = 6,
    scorer: StructuredTopicScorer | Callable[..., Mapping[str, Any]] | None = None,
) -> ResearchBundle:
    # Approximate clustering must not invalidate IDs returned to the agent.
    sources = tuple({item.source_id: item for item in map(to_source_evidence, values)}.values())
    stories = tuple(cluster_stories(
        sources,
        query=query,
        topics=topics,
        recent_posts=recent_posts,
        channel_evidence_ids=channel_evidence_ids,
        channel_evidence=channel_evidence,
        now=now,
        scorer=scorer,
    ))
    selected = tuple(source.source_id for source in select_sources(
        sources,
        query=query,
        topics=topics,
        recent_posts=recent_posts,
        max_sources=max_selected_sources,
        now=now,
        scorer=scorer,
    ))
    warnings: list[str] = []
    if not sources:
        warnings.append("No accessible sources were found for this request.")
    if any("single_source" in story.conflict_flags for story in stories):
        warnings.append("Some stories have single-source coverage.")
    if any("possible_conflict" in story.conflict_flags or "conflicting_numbers" in story.conflict_flags for story in stories):
        warnings.append("Some sources may disagree; review conflict flags before drafting.")
    return ResearchBundle(
        query=query,
        sources=sources,
        stories=stories,
        warnings=tuple(warnings),
        retrieved_at=_aware(now) or _now(),
        selected_source_ids=selected,
    )


def story_from_dict(value: Mapping[str, Any]) -> StoryCluster:
    """Rehydrate a persisted story without trusting unbounded JSON fields."""

    return StoryCluster(
        cluster_id=str(value.get("cluster_id") or value.get("id") or "")[:128],
        headline=str(value.get("headline") or "")[:1_000],
        summary=str(value.get("summary") or "")[:2_000],
        source_ids=tuple(str(item)[:128] for item in (value.get("source_ids") or [])[:12]),
        primary_source_id=str(value.get("primary_source_id") or "")[:128] or None,
        supporting_source_ids=tuple(str(item)[:128] for item in (value.get("supporting_source_ids") or [])[:12]),
        score=float(value.get("score") or 0.0),
        topic_relevance=float(value.get("topic_relevance") or 0.0),
        freshness=float(value.get("freshness") or 0.0),
        source_quality=float(value.get("source_quality") or 0.0),
        novelty=float(value.get("novelty") or 0.0),
        status=str(value.get("status") or "new") if str(value.get("status") or "new") in {"new", "saved", "dismissed", "used"} else "new",
        matched_topics=tuple(str(item)[:120] for item in (value.get("matched_topics") or [])[:20]),
        conflict_flags=tuple(str(item)[:80] for item in (value.get("conflict_flags") or [])[:20]),
        warnings=tuple(str(item)[:300] for item in (value.get("warnings") or [])[:20]),
        channel_evidence_ids=tuple(int(item) for item in (value.get("channel_evidence_ids") or [])[:20]),
        published_at=_aware(value.get("published_at")),
        retrieved_at=_aware(value.get("retrieved_at")) or _now(),
        score_breakdown=dict(value.get("score_breakdown") or {}),
        relevance_features=dict(value.get("relevance_features") or {}),
        novelty_features=dict(value.get("novelty_features") or {}),
        channel_evidence=_channel_evidence(value.get("channel_evidence") or []),
    )


# Compatibility names used by the spec and future persistence adapters.
SourceRecord = SourceEvidence
Story = StoryCluster
dedupe_sources = deduplicate_sources
canonical_source_url = canonicalize_url
cluster_sources = cluster_stories
rank_stories = rank_sources
normalize_url = canonicalize_url
canonicalize_source_url = canonicalize_url
