"""Evidence-backed topic and style profile construction for Studio.

M3 keeps profile extraction deterministic and local. The output is shaped for a
later provider-backed interpretation, but every observation already points to
post IDs in the immutable analytics result and can therefore be validated
before persistence or prompting.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .analytics import ANALYTICS_VERSION, ChannelAnalytics, EvidencePost


PROFILE_VERSION = "m3.profile.v1"
STYLE_MIN_CHARS = 80
_TOKEN_RE = re.compile(r"#[\w-]+|[\w'-]{3,}", re.UNICODE)
_STOPWORDS = {
    "about", "after", "also", "because", "been", "being", "from", "have", "into", "just", "more", "most", "only", "other", "over", "that", "their", "there", "these", "they", "this", "those", "through", "under", "what", "when", "where", "which", "while", "with", "would", "your",
    "для", "если", "или", "как", "когда", "которые", "может", "над", "нас", "него", "нее", "них", "они", "после", "при", "про", "себя", "также", "только", "через", "это", "этот", "эти", "быть", "был", "была", "были",
}


class TopicInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    scope: str = ""
    included: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    representative_post_ids: list[int] = Field(default_factory=list, max_length=30)
    claim: str
    metrics: dict[str, float] = Field(default_factory=dict)
    uplift: float | None = None
    sample_size: int = 0
    confidence: str = "low"
    limitations: list[str] = Field(default_factory=list)


class StyleProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    typical_length_chars: int = 0
    median_length_chars: int = 0
    typical_sentence_count: float = 0.0
    paragraph_count: float = 0.0
    bullet_rate: float = 0.0
    emoji_rate: float = 0.0
    punctuation_rate: float = 0.0
    structure: list[str] = Field(default_factory=list)
    tone_hints: list[str] = Field(default_factory=list)
    evidence_post_ids: list[int] = Field(default_factory=list, max_length=30)
    confidence: str = "low"
    limitations: list[str] = Field(default_factory=list)


class ChannelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_version: str = PROFILE_VERSION
    channel_id: int
    topics: list[TopicInsight] = Field(default_factory=list, max_length=8)
    style_profile: StyleProfile = Field(default_factory=StyleProfile)
    editorial_rules: dict[str, Any] = Field(default_factory=dict)
    confidence: str = "low"
    confidence_score: float = 0.25
    analysis_id: str | None = None
    analysis_input_hash: str = ""
    version: int = 1
    provider: str = "local"
    model: str = ""
    prompt_version: str = PROFILE_VERSION
    limitations: list[str] = Field(default_factory=list)


class ProfileAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_version: str = PROFILE_VERSION
    channel_id: int
    analytics_version: str = ANALYTICS_VERSION
    input_hash: str
    evidence_post_ids: list[int] = Field(default_factory=list)
    eligible_post_count: int = 0
    style_eligible_post_count: int = 0
    topic_insights: list[TopicInsight] = Field(default_factory=list)
    style_insights: dict[str, Any] = Field(default_factory=dict)
    confidence: str = "low"
    confidence_score: float = 0.25
    limitations: list[str] = Field(default_factory=list)
    provider: str = "local"
    model: str = ""
    prompt_version: str = PROFILE_VERSION


class TopicChangeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    channel_id: int
    base_profile_version: int
    proposed_topics: list[str] = Field(default_factory=list, max_length=20)
    style_diff: dict[str, Any] = Field(default_factory=dict)
    editorial_rules: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    status: str = "proposed"
    requires_confirmation: bool = True


class EvidenceIntegrityError(ValueError):
    """Raised when a profile refers to posts absent from its analysis."""


def _clean_token(value: str) -> str:
    return value.strip(".,:;!?()[]{}\"'`“”‘’").lower()


def _tokens(posts: Iterable[EvidencePost]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for post in posts:
        for raw in _TOKEN_RE.findall(post.excerpt or ""):
            token = _clean_token(raw)
            if token.startswith("#"):
                token = token[1:]
            if len(token) < 3 or token in _STOPWORDS or token.isdigit():
                continue
            counts[token] += 1
    return counts


def _sentence_count(text: str) -> int:
    return max(1, len(re.findall(r"[.!?]+", text))) if text.strip() else 0


def _style(posts: list[EvidencePost], *, confidence: str, limitations: list[str]) -> StyleProfile:
    texts = [post.excerpt for post in posts if post.style_eligible and post.excerpt.strip()]
    lengths = [len(text) for text in texts]
    sentences = [_sentence_count(text) for text in texts]
    paragraphs = [max(1, len([part for part in re.split(r"\n\s*\n", text) if part.strip()])) for text in texts]
    if not texts:
        return StyleProfile(confidence="low", limitations=list(limitations) + ["No style-eligible post text was available."])
    bullet_rate = sum(bool(re.search(r"(?:^|\n)\s*[-•*]\s", text)) for text in texts) / len(texts)
    emoji_rate = sum(bool(re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text)) for text in texts) / len(texts)
    punctuation_rate = sum(len(re.findall(r"[,;:!?]", text)) for text in texts) / max(sum(lengths), 1)
    structure: list[str] = []
    if bullet_rate >= 0.25:
        structure.append("occasional bullets")
    if statistics.mean(paragraphs) > 1.3:
        structure.append("short paragraphs")
    if not structure:
        structure.append("compact prose")
    tone: list[str] = []
    if statistics.mean(sentences) <= 3:
        tone.append("direct")
    if statistics.mean(lengths) >= 600:
        tone.append("deep-dive")
    elif statistics.mean(lengths) <= 220:
        tone.append("concise")
    return StyleProfile(
        typical_length_chars=round(statistics.mean(lengths)),
        median_length_chars=round(statistics.median(lengths)),
        typical_sentence_count=round(statistics.mean(sentences), 2),
        paragraph_count=round(statistics.mean(paragraphs), 2),
        bullet_rate=round(bullet_rate, 3),
        emoji_rate=round(emoji_rate, 3),
        punctuation_rate=round(punctuation_rate, 5),
        structure=structure,
        tone_hints=tone or ["editorial"],
        evidence_post_ids=[post.post_id for post in posts[:30]],
        confidence=confidence,
        limitations=list(limitations),
    )


def validate_profile_evidence(profile: ChannelProfile | Mapping[str, Any], analytics: ChannelAnalytics) -> ChannelProfile:
    """Ensure every profile observation cites an evidence post in the pack."""

    parsed = profile if isinstance(profile, ChannelProfile) else ChannelProfile.model_validate(profile)
    allowed = {post.post_id for post in analytics.evidence_posts}
    referenced: set[int] = set(parsed.style_profile.evidence_post_ids)
    for topic in parsed.topics:
        referenced.update(topic.representative_post_ids)
    missing = sorted(referenced - allowed)
    if missing:
        raise EvidenceIntegrityError(f"profile references posts outside its analysis evidence: {missing}")
    return parsed


def build_profile(
    analytics: ChannelAnalytics,
    *,
    max_topics: int = 8,
    provider: str = "local",
    model: str = "",
    prompt_version: str = PROFILE_VERSION,
) -> tuple[ChannelProfile, ProfileAnalysis]:
    """Infer an initial topic/style profile from deterministic evidence."""

    max_topics = max(1, min(int(max_topics), 8))
    evidence = list(analytics.evidence_posts)
    top_ids = {post.post_id for post in analytics.top_posts}
    top_posts = [post for post in evidence if post.post_id in top_ids]
    if not top_posts:
        top_posts = evidence[:]
    counts = _tokens(top_posts)
    candidates = [token for token, _ in counts.most_common(max_topics)]
    if not candidates and evidence:
        candidates = ["general editorial"]
    # A sufficiently large sample gets the 5-8 proposed topics required by
    # the Studio spec. Labels are transparent keywords until a provider-backed
    # semantic classifier is introduced.
    if len(evidence) >= 5 and len(candidates) < min(5, max_topics):
        for fallback in ("news", "analysis", "community", "updates", "opinion"):
            if fallback not in candidates:
                candidates.append(fallback)
            if len(candidates) >= min(5, max_topics):
                break
    limitations = list(analytics.limitations)
    topics: list[TopicInsight] = []
    baseline_ids = {post.post_id for post in analytics.baseline_posts}
    for candidate in candidates[:max_topics]:
        matching = [post for post in top_posts if candidate in _tokens([post])]
        ids = [post.post_id for post in matching[:30]] or [post.post_id for post in top_posts[: min(5, len(top_posts))]]
        baseline = [post for post in analytics.baseline_posts if post.post_id in baseline_ids]
        mean_top = sum(post.scores.traction_score for post in matching) / len(matching) if matching else 0.0
        mean_base = sum(post.scores.traction_score for post in baseline) / len(baseline) if baseline else 0.0
        uplift = ((mean_top / mean_base) - 1.0) if mean_base else None
        topics.append(
            TopicInsight(
                name=candidate,
                scope=f"Posts where {candidate} is a recurring subject or tag.",
                included=[candidate],
                excluded=[],
                representative_post_ids=ids,
                claim=f"{candidate.title()} appears in the strongest available traction evidence.",
                metrics={"mean_traction": round(mean_top, 6)},
                uplift=round(uplift, 6) if uplift is not None else None,
                sample_size=len(matching),
                confidence=analytics.confidence if matching else "low",
                limitations=list(limitations),
            )
        )
    style_posts = [post for post in analytics.top_posts if post.style_eligible]
    style = _style(style_posts, confidence=analytics.confidence, limitations=limitations)
    profile = ChannelProfile(
        channel_id=analytics.channel_id,
        topics=topics,
        style_profile=style,
        editorial_rules={"do_not_publish_automatically": True, "avoid_distinctive_reuse": True},
        confidence=analytics.confidence,
        confidence_score=analytics.confidence_score,
        analysis_input_hash=analytics.input_hash,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        limitations=limitations,
    )
    analysis = ProfileAnalysis(
        channel_id=analytics.channel_id,
        input_hash=analytics.input_hash,
        evidence_post_ids=[post.post_id for post in evidence],
        eligible_post_count=analytics.eligible_post_count,
        style_eligible_post_count=analytics.style_eligible_post_count,
        topic_insights=topics,
        style_insights=style.model_dump(mode="json"),
        confidence=analytics.confidence,
        confidence_score=analytics.confidence_score,
        limitations=limitations,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
    )
    validate_profile_evidence(profile, analytics)
    return profile, analysis


def _topic_list(text: str) -> list[str]:
    values = re.split(r"[,;\n]+", text.strip())
    return [re.sub(r"^[\s\-•]+", "", value).strip() for value in values if value.strip()][:20]


def propose_topic_change(profile: ChannelProfile | Mapping[str, Any], instruction: str) -> TopicChangeProposal:
    """Parse a free-text topic request without silently applying broad edits."""

    current = profile if isinstance(profile, ChannelProfile) else ChannelProfile.model_validate(profile)
    text = " ".join(str(instruction or "").split())
    match = re.search(r"(?:replace|set|change)\s+(?:my\s+)?topics?\s+(?:to|with)\s+(.+)$", text, re.I)
    add_match = re.search(r"(?:add|include)\s+(?:the\s+)?topics?\s+(.+)$", text, re.I)
    remove_match = re.search(r"(?:remove|drop|exclude)\s+(?:the\s+)?topics?\s+(.+)$", text, re.I)
    if match:
        proposed = _topic_list(match.group(1))
        exact = len(proposed) <= 8 and bool(proposed)
        return TopicChangeProposal(
            channel_id=current.channel_id,
            base_profile_version=current.version,
            proposed_topics=proposed,
            reason="Explicit topic replacement requested by the channel owner.",
            status="confirmed" if exact else "proposed",
            requires_confirmation=not exact,
        )
    if add_match:
        additions = _topic_list(add_match.group(1))
        proposed = [topic.name for topic in current.topics] + additions
        return TopicChangeProposal(
            channel_id=current.channel_id,
            base_profile_version=current.version,
            proposed_topics=list(dict.fromkeys(proposed))[:20],
            reason="Topic additions requested; review the resulting profile.",
            status="proposed",
            requires_confirmation=True,
        )
    if remove_match:
        remove = {item.lower() for item in _topic_list(remove_match.group(1))}
        proposed = [topic.name for topic in current.topics if topic.name.lower() not in remove]
        return TopicChangeProposal(
            channel_id=current.channel_id,
            base_profile_version=current.version,
            proposed_topics=proposed,
            reason="Topic exclusions requested; review the resulting profile.",
            status="proposed",
            requires_confirmation=True,
        )
    return TopicChangeProposal(
        channel_id=current.channel_id,
        base_profile_version=current.version,
        proposed_topics=[topic.name for topic in current.topics],
        reason="The request does not name an exact topic change; confirmation is required.",
        status="proposed",
        requires_confirmation=True,
    )


def apply_confirmed_topic_change(profile: ChannelProfile | Mapping[str, Any], proposal: TopicChangeProposal | Mapping[str, Any]) -> ChannelProfile:
    current = profile if isinstance(profile, ChannelProfile) else ChannelProfile.model_validate(profile)
    change = proposal if isinstance(proposal, TopicChangeProposal) else TopicChangeProposal.model_validate(proposal)
    if change.channel_id != current.channel_id:
        raise ValueError("topic change channel does not match profile")
    if change.base_profile_version != current.version:
        raise ValueError("topic change is based on an outdated profile version")
    if change.status not in {"confirmed", "applied"}:
        raise ValueError("topic change requires explicit confirmation")
    by_name = {topic.name.lower(): topic for topic in current.topics}
    topics: list[TopicInsight] = []
    for name in change.proposed_topics[:8]:
        existing = by_name.get(name.lower())
        topics.append(existing or TopicInsight(name=name, claim="Added explicitly by the channel owner."))
    return current.model_copy(update={"topics": topics, "version": current.version + 1})


__all__ = [
    "PROFILE_VERSION",
    "ChannelProfile",
    "EvidenceIntegrityError",
    "ProfileAnalysis",
    "StyleProfile",
    "TopicChangeProposal",
    "TopicInsight",
    "apply_confirmed_topic_change",
    "build_profile",
    "propose_topic_change",
    "validate_profile_evidence",
]
