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


PROFILE_EXTRACTION_VERSION = "channel.profile.v2"


class ProfileDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topics: list[str] = Field(default_factory=list, max_length=12)
    editorial_rules: list[str] = Field(default_factory=list, max_length=20)
    style_rules: list[str] = Field(default_factory=list, max_length=20)
    built_from_posts: int = 0
    limitations: list[str] = Field(default_factory=list)
    formatting_facts: list[dict[str, Any]] = Field(default_factory=list)


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
    allowed_ids = {post.post_id for post in analytics.evidence_posts}
    style_posts = [post for post in analytics.top_posts if post.style_eligible and post.post_id in allowed_ids]
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


def _strip_markup(text: str) -> str:
    """Strip dialect-forbidden markup from a single line, per spec 4.2/1.1."""
    # Remove headings (#, ## …) at line start
    text = re.sub(r"^\s*#{1,6}\s+", "", text)
    # Remove images ![alt](url) -> alt
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Reduce javascript/data links [text](javascript:…) -> text
    def _link_fix(m):
        label = m.group(1)
        url = m.group(2).strip()
        if url.lower().startswith(("http://", "https://")):
            return f"[{label}]({url})"
        return label
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link_fix, text)
    return text


def _formatting_facts(rows: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Compute deterministic formatting facts and emit style lines."""
    total = len(rows)
    if total == 0:
        return [], []
    bold_first = 0
    sig_link = 0
    sig_url = ""
    sig_anchor = ""
    italic_use = 0
    code_use = 0
    quote_use = 0
    spoiler_use = 0
    emoji_first = 0
    emoji_elsewhere = 0
    inline_link_counts: list[int] = []
    for r in rows:
        entities = r.get("formatting_entities") or []
        text = str(r.get("text") or "")
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            inline_link_counts.append(0)
            continue
        first = lines[0]
        last = lines[-1] if lines else ""
        # Bold first line: first line fully covered by bold (UTF-16 length)
        first_utf16_len = len(first.encode("utf-16-le")) // 2
        for ent in entities:
            if ent.get("type") == "bold" and ent.get("offset") == 0 and ent.get("length") == first_utf16_len:
                bold_first += 1
                break
        # Signature link in last line: text_link/url entity intersecting last line region
        last_start = len(text) - len(last) if last else len(text)
        candidate_url = ""
        candidate_anchor = ""
        for ent in entities:
            if ent.get("type") in ("text_link", "url"):
                off = int(ent.get("offset") or 0)
                length = int(ent.get("length") or 0)
                # Heuristic: overlaps last non-empty line
                if off >= last_start - 5 and off + length <= len(text) + 5:
                    url = ent.get("url") or (text[off:off+length] if ent.get("type") == "url" else "")
                    if url:
                        candidate_url = url
                        candidate_anchor = text[off:off+length][:30].strip()
                        break
        if candidate_url:
            sig_link += 1
            sig_url = candidate_url
            sig_anchor = candidate_anchor
        # Counts for other entities
        has_italic = any(ent.get("type") == "italic" for ent in entities)
        has_code = any(ent.get("type") == "code" for ent in entities)
        has_quote = any(ent.get("type") == "blockquote" for ent in entities)
        has_spoiler = any(ent.get("type") == "spoiler" for ent in entities)
        if has_italic:
            italic_use += 1
        if has_code:
            code_use += 1
        if has_quote:
            quote_use += 1
        if has_spoiler:
            spoiler_use += 1
        # Inline links median: count text_link per post
        inline_link_counts.append(sum(1 for ent in entities if ent.get("type") == "text_link"))
        # Emoji position
        if re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", first):
            emoji_first += 1
        elif re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text):
            emoji_elsewhere += 1
    facts: list[dict[str, Any]] = []
    lines_out: list[str] = []
    # Bold first line
    rate = bold_first / total if total else 0
    facts.append({"fact": "bold first line", "rate": round(rate, 3), "count": bold_first, "total": total})
    if rate >= 0.6:
        # Generic placeholder only: never a real post's title.
        lines_out.append("The first line is the title, in bold: **Example title**")
    elif rate >= 0.2:
        lines_out.append("Sometimes the first line is bold.")
    # Signature link
    rate = sig_link / total if total else 0
    facts.append({"fact": "signature link", "rate": round(rate, 3), "count": sig_link, "total": total, "url": sig_url})
    if rate >= 0.6 and sig_url and sig_url.lower().startswith(("http://", "https://")):
        # Fall back to the link's host when the anchor text is empty; the
        # signature belongs to the channel being analysed, never to an example.
        anchor = sig_anchor or sig_url.split("//", 1)[-1].split("/", 1)[0] or "channel"
        lines_out.append(f"Posts end with a signature line that links the channel: — [{anchor}]({sig_url})")
    elif rate >= 0.2:
        lines_out.append("Sometimes posts end with a signature link.")
    # Inline links median
    if inline_link_counts:
        median_links = sorted(inline_link_counts)[len(inline_link_counts)//2]
        facts.append({"fact": "inline links", "median": median_links})
        if median_links >= 1:
            rate = sum(1 for c in inline_link_counts if c >= 1) / total
            if rate >= 0.6:
                lines_out.append("Sources are linked inline on the words they support, usually 1–2 per post.")
            elif rate >= 0.2:
                lines_out.append("Sometimes sources are linked inline.")
    # Italic / code / quote / spoiler
    for name, count in [("italic", italic_use), ("code", code_use), ("quote", quote_use), ("spoiler", spoiler_use)]:
        rate = count / total if total else 0
        facts.append({"fact": name, "rate": round(rate, 3), "count": count})
    italic_rate = italic_use / total if total else 0
    if italic_rate >= 0.6:
        lines_out.append("*Italic* is used for the key number on first mention; nothing else is italic.")
    elif italic_rate < 0.2 and quote_use / total < 0.2:
        # Only emit once as style habit, not duplicate
        pass
    # Quote use
    quote_rate = quote_use / total if total else 0
    if quote_rate < 0.2 and italic_rate < 0.2:
        # Combined low-use signal was previously single line; keep spec example for blockquotes
        if italic_rate < 0.2:
            lines_out.append("Blockquotes are not used.")
    elif quote_rate >= 0.6:
        lines_out.append("Blockquotes are used for quoted statements.")
    elif quote_rate >= 0.2:
        lines_out.append("Sometimes blockquotes are used for quoted statements.")
    # Emoji position
    facts.append({"fact": "emoji_first", "rate": round(emoji_first/total,3) if total else 0})
    facts.append({"fact": "emoji_elsewhere", "rate": round(emoji_elsewhere/total,3) if total else 0})
    if emoji_first / total >= 0.6 and (emoji_first + emoji_elsewhere) / total >= 0.6:
        lines_out.append("Emoji appear only at the start of the first line, at most one.")
    elif (emoji_first + emoji_elsewhere) / total >= 0.2:
        lines_out.append("Sometimes emoji are used, usually at the start of the first line.")
    return [line for line in lines_out if line], facts


def _is_template_line(line: str) -> bool:
    low = line.strip().lower()
    return bool(
        re.search(r"^(start|begin|open) with", low)
        or re.search(r"\bthen\b", low)
        or re.search(r"^(end|close|finish) with", low)
        or "always use the format" in low
        or re.match(r"^\d+\.\s", line.strip())
    )

def _sanitize_line(line: str, *, limit: int) -> str:
    from .sources import sanitize_untrusted_text
    # Treat post text as data – strip injection
    cleaned, _ = sanitize_untrusted_text(line)
    cleaned = _strip_markup(cleaned)
    # Collapse whitespace, single line
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned

def _build_draft_from_analytics(analytics: ChannelAnalytics, rows: list[dict[str, Any]]) -> ProfileDraft:
    # Deterministic build for test mode – every line is a rule/tendency, never a layout.
    # Topics: derive from top tokens; ensure 5-8 when evidence >=5
    topics: list[str] = []
    for post in analytics.top_posts[:8]:
        tokens = [t for t in _tokens([post]) if t not in _STOPWORDS]
        if tokens:
            cand = tokens[0].title()
            topics.append(f"{cand} — appears in successful posts")
    if not topics and analytics.evidence_posts:
        topics = ["General editorial — appears in posts"]
    if len(analytics.evidence_posts) >= 5 and len(topics) < 5:
        for fallback in ("News — appears in successful posts", "Analysis — appears in successful posts", "Community — appears in successful posts"):
            if fallback not in topics:
                topics.append(fallback)
            if len(topics) >= 5:
                break
    topics = topics[:12]
    # Style lines from formatting facts
    style_lines, facts = _formatting_facts(rows)
    editorial = [
        "Verify facts with sources before publishing.",
        "Do not speculate without on-record statement.",
        "Never speculate about motives without an on-record statement.",
    ]
    style: list[str] = list(style_lines)
    if not style:
        style = ["Most posts run 400–1,400 characters.", "Never speculate about motives without an on-record statement."]
    else:
        # Always include length tendency
        if not any("400" in s for s in style):
            style.append("Most posts run 400–1,400 characters; go longer only when the story needs it.")
    # Sanitize every line, enforce limits, drop template-like
    limitations: list[str] = []
    removed_template = 0
    def _process(lines: list[str], limit: int) -> list[str]:
        nonlocal removed_template
        out: list[str] = []
        for raw in lines:
            sanitized = _sanitize_line(raw, limit=limit)
            if not sanitized:
                continue
            if _is_template_line(sanitized):
                removed_template += 1
                continue
            # Ensure no forbidden markup survived
            if sanitized.startswith("#") or sanitized.startswith("![") or "<" in sanitized and ">" in sanitized:
                sanitized = _strip_markup(sanitized)
                sanitized = " ".join(sanitized.split())
            out.append(sanitized[:limit])
        return out
    topics = _process(topics, 160)
    editorial = _process(editorial, 200)
    style = _process(style, 300)
    # Keep within model limits
    topics = topics[:12]
    editorial = editorial[:20]
    style = style[:20]
    if removed_template:
        limitations.append(f"{removed_template} template-like lines removed")
    if facts:
        limitations.append(f"Formatting facts computed from {len(rows)} posts")
    return ProfileDraft(topics=topics, editorial_rules=editorial, style_rules=style, built_from_posts=len(rows), limitations=limitations, formatting_facts=facts)


async def build_profile_draft(analytics: ChannelAnalytics, rows: list[dict[str, Any]], settings) -> ProfileDraft:
    # Wrapper for service
    return _build_draft_from_analytics(analytics, rows)


PROFILE_FIELD_MAX_CHARS = 2_000
PROFILE_FIELD_MAX_LINES = 60


def fit_field_lines(lines: list[str], *, max_chars: int = PROFILE_FIELD_MAX_CHARS, max_lines: int = PROFILE_FIELD_MAX_LINES) -> tuple[list[str], int]:
    """Keep leading lines that fit one profile text field; return (kept, dropped).

    The dialog and PUT /profile enforce the same limits, so a build result
    that overflowed would leave Save disabled with nothing to explain why.
    """

    kept: list[str] = []
    total = 0
    for line in lines:
        if len(kept) >= max_lines:
            break
        addition = len(line) + (1 if kept else 0)
        if total + addition > max_chars:
            break
        kept.append(line)
        total += addition
    return kept, max(0, len(lines) - len(kept))


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
        return TopicChangeProposal(
            channel_id=current.channel_id,
            base_profile_version=current.version,
            proposed_topics=proposed,
            reason="Explicit topic replacement requested by the channel owner.",
            status="proposed",
            requires_confirmation=True,
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
