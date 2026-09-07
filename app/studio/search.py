"""Provider-neutral, bounded web search for the Studio.

The application owns this small adapter instead of exposing SearXNG response
objects to the agent or browser.  Only normalized metadata and provenance are
returned.  A provider outage is a typed degraded result, not an exception that
can interrupt Telegram collection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .search_health import configured_search_state

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_",
}
_ALLOWED_CATEGORIES = {"general", "news", "science", "technology", "politics", "world"}
_TOKEN_RE = re.compile(r"[\w\u0400-\u04ff\u00c0-\u024f]{3,}", re.UNICODE)
_QUERY_STOPWORDS = {
    "about", "after", "also", "and", "are", "can", "current", "find", "for", "from", "how", "latest", "more", "news", "please", "show", "that", "the", "this", "use", "what", "with",
    "все", "для", "или", "как", "мне", "найди", "нужно", "новости", "пожалуйста", "про", "свежие", "что", "это",
}
_QUERY_CONCEPTS = (
    ("open_source", re.compile(r"\bopen[- ]source\b|\bопенсорс\w*\b", re.IGNORECASE), re.compile(r"\bopen[- ]source\b|\bопенсорс\w*\b|github\.com/.+/(?:releases?|tree|blob)", re.IGNORECASE)),
    ("open_weights", re.compile(r"\bopen[- ]weights?\b|\bоткрыт\w*\s+вес\w*\b", re.IGNORECASE), re.compile(r"\bopen[- ]weights?\b|\bоткрыт\w*\s+вес\w*\b", re.IGNORECASE)),
    ("agent", re.compile(r"\bagentic?\b|\bagents?\b|\bагент\w*\b", re.IGNORECASE), re.compile(r"\bagentic?\b|\bagents?\b|\bагент\w*\b", re.IGNORECASE)),
    ("llm", re.compile(r"\bllms?\b|\blarge language models?\b|\bязыков\w*\s+модел\w*\b", re.IGNORECASE), re.compile(r"\bllms?\b|\blarge language models?\b|\bязыков\w*\s+модел\w*\b", re.IGNORECASE)),
    ("ai", re.compile(r"\bai\b|\bartificial intelligence\b|\bии\b", re.IGNORECASE), re.compile(r"\bai\b|\bartificial intelligence\b|\bmachine learning\b|\bии\b|\bискусственн\w*\s+интеллект\w*\b", re.IGNORECASE)),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def _clean_text(value: Any, *, limit: int) -> str:
    text = _CONTROL_RE.sub(" ", str(value or ""))
    return " ".join(text.split())[:limit].strip()


def _query_terms(value: str) -> tuple[str, ...]:
    """Return the discriminative terms that a result must substantively match.

    A metasearch engine can legally return broad entity landing pages for a
    narrow request.  Those pages are not evidence for a Studio recommendation,
    so this is intentionally a conservative lexical guard rather than another
    opaque model judgement.  Prefix matching keeps ordinary Russian inflection
    from making a relevant result disappear.
    """

    terms = []
    for token in _TOKEN_RE.findall(str(value or "").lower()):
        if token in _QUERY_STOPWORDS or token.isdigit():
            continue
        if token not in terms:
            terms.append(token)
    return tuple(terms[:12])


def _term_matches(term: str, candidates: set[str]) -> bool:
    if term in candidates:
        return True
    # Russian and other inflected words share a useful stem after five
    # characters; do not apply this to short terms such as "api".
    return len(term) >= 6 and any(candidate.startswith(term[:6]) or term.startswith(candidate[:6]) for candidate in candidates if len(candidate) >= 6)


def _result_relevance(*, query: str, title: str, snippet: str, url: str) -> tuple[float, int, int]:
    """Score transparent lexical query coverage for a normalized result."""

    terms = _query_terms(query)
    if not terms:
        return 1.0, 0, 0
    title_tokens = set(_TOKEN_RE.findall(title.lower()))
    snippet_tokens = set(_TOKEN_RE.findall(snippet.lower()))
    url_tokens = set(_TOKEN_RE.findall(url.lower().replace("-", " ").replace("/", " ")))
    matched = sum(
        1
        for term in terms
        if _term_matches(term, title_tokens) or _term_matches(term, snippet_tokens) or _term_matches(term, url_tokens)
    )
    title_matched = sum(1 for term in terms if _term_matches(term, title_tokens))
    # Prefer a match in the headline, but retain a sufficiently specific
    # excerpt-only result. The score is recorded as provenance, never hidden.
    score = min(1.0, (matched + (0.5 * title_matched)) / max(1, len(terms)))
    return score, matched, len(terms)


def _missing_query_concepts(*, query: str, title: str, snippet: str, url: str) -> tuple[str, ...]:
    """Reject category collisions such as an Audi model for an AI-model query."""

    haystack = f"{title} {snippet} {url}"
    return tuple(name for name, query_pattern, result_pattern in _QUERY_CONCEPTS if query_pattern.search(query) and not result_pattern.search(haystack))


def _metadata_datetime(value: Any, *, now: datetime) -> datetime | None:
    """Extract dates SearXNG engines place in their display metadata field."""

    raw = _clean_text(value, limit=200)
    match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", raw)
    if match:
        return _parse_datetime(f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}")
    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", raw)
    if match:
        return _parse_datetime(f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}")
    relative = re.search(r"\b(\d{1,3})\s+days?\s+ago\b", raw, re.IGNORECASE)
    if relative:
        from datetime import timedelta

        return now - timedelta(days=int(relative.group(1)))
    return None


def _minimum_required_matches(term_count: int) -> int:
    if term_count <= 2:
        return 1
    # A detailed natural-language query has many descriptive words. Requiring
    # every word would reject good reporting, but a landing page matching only
    # the company name must not pass.
    return min(3, max(2, (term_count + 2) // 3))


def _normalize_engines(value: Any) -> tuple[str, ...]:
    names = []
    raw_values = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    for raw in raw_values:
        name = raw.strip().lower()
        if name and re.fullmatch(r"[a-z0-9_. -]{1,64}", name) and name not in names:
            names.append(name)
    return tuple(names[:12])


def canonicalize_url(value: str) -> str:
    """Normalize a source URL without fetching it.

    Fragments and common analytics parameters are not source identity.  The
    function intentionally does not follow redirects or make DNS calls.
    Invalid/non-web URLs return an empty string and are discarded by search.
    """

    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username or parsed.password:
            return ""
        host = parsed.hostname.lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError:
            return ""
        if (parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443):
            port = None
        # URL authority syntax requires brackets around IPv6 literals.
        authority_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        netloc = authority_host if port is None else f"{authority_host}:{port}"
        path = parsed.path or "/"
        if path != "/":
            path = re.sub(r"/{2,}", "/", path)
            path = path.rstrip("/") or "/"
        query_pairs = []
        for key, val in parse_qsl(parsed.query, keep_blank_values=True):
            lowered = key.lower()
            if lowered.startswith("utm_") or lowered in _TRACKING_PARAMS:
                continue
            query_pairs.append((key, val))
        query_pairs.sort()
        return urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(query_pairs), ""))
    except (TypeError, ValueError):
        return ""


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = _clean_text(value, limit=120)
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        # SearXNG engines occasionally return a date without a time zone.
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Normalized search inputs accepted by every provider implementation."""

    text: str
    language: str | None = None
    recency_days: int | None = None
    categories: tuple[str, ...] = ("general", "news")
    domains: tuple[str, ...] = ()
    engines: tuple[str, ...] = ()
    excluded_domains: tuple[str, ...] = ()
    limit: int | None = None

    @property
    def query(self) -> str:
        """Compatibility alias used by provider-neutral callers."""

        return self.text


@dataclass(frozen=True, slots=True)
class SearchResult:
    url: str
    canonical_url: str
    title: str
    snippet: str
    source_name: str
    domain: str
    published_at: datetime | None
    provider: str
    query: str
    fetched_at: datetime
    result_index: int
    source_id: str
    provenance: dict[str, Any] = field(default_factory=dict)
    injection_flags: tuple[str, ...] = ()

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        data = {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "snippet": self.snippet,
            "source_name": self.source_name,
            "domain": self.domain,
            "published_at": _iso(self.published_at),
            "provider": self.provider,
            "query": self.query,
            "fetched_at": _iso(self.fetched_at),
            "result_index": self.result_index,
            "source_id": self.source_id,
            "provenance": dict(self.provenance),
        }
        return data

    as_dict = model_dump


@dataclass(frozen=True, slots=True)
class SearchResponse:
    query: SearchQuery
    results: tuple[SearchResult, ...] = ()
    provider: str = "searxng"
    fetched_at: datetime = field(default_factory=_utcnow)
    cache_hit: bool = False
    degraded: bool = False
    warnings: tuple[str, ...] = ()
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "query": self.query.text,
            "fetched_at": _iso(self.fetched_at),
            "cache_hit": self.cache_hit,
            "trace_id": self.trace_id,
            "categories": list(self.query.categories),
            "domains": list(self.query.domains),
            "engines": list(self.query.engines),
            "excluded_domains": list(self.query.excluded_domains),
            "recency_days": self.query.recency_days,
        }

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return {
            "query": {
                "text": self.query.text,
                "language": self.query.language,
                "recency_days": self.query.recency_days,
                "categories": list(self.query.categories),
                "domains": list(self.query.domains),
                "engines": list(self.query.engines),
                "excluded_domains": list(self.query.excluded_domains),
                "limit": self.query.limit,
            },
            "results": [item.model_dump(mode=mode) for item in self.results],
            "provider": self.provider,
            "fetched_at": _iso(self.fetched_at),
            "cache_hit": self.cache_hit,
            "degraded": self.degraded,
            "warnings": list(self.warnings),
            "trace_id": self.trace_id,
            "provenance": self.provenance,
        }

    as_dict = model_dump


class SearchProvider(Protocol):
    """Stable application-owned search boundary."""

    async def search(self, query: SearchQuery | str, **kwargs: Any) -> SearchResponse: ...


class SearchProviderError(RuntimeError):
    """An internal provider failure with no upstream payload attached."""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.retryable = retryable


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    response: SearchResponse


class SearchCache:
    """Small process-local TTL cache; only normalized metadata is retained."""

    def __init__(self, *, ttl_seconds: float = 900.0, max_entries: int = 256) -> None:
        self.ttl_seconds = max(0.0, min(float(ttl_seconds), 86_400.0))
        self.max_entries = max(1, min(int(max_entries), 2_048))
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> SearchResponse | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                self._entries.pop(key, None)
                return None
            return replace(entry.response, cache_hit=True)

    async def put(self, key: str, response: SearchResponse) -> None:
        if self.ttl_seconds <= 0:
            return
        async with self._lock:
            self._entries[key] = _CacheEntry(time.monotonic() + self.ttl_seconds, replace(response, cache_hit=False))
            while len(self._entries) > self.max_entries:
                self._entries.pop(next(iter(self._entries)))

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()


def _normalize_query(value: SearchQuery | str, *, max_results: int, max_queries: int = 3, **kwargs: Any) -> SearchQuery:
    if isinstance(value, SearchQuery):
        query = value
    else:
        query = SearchQuery(
            text=str(value or ""),
            language=kwargs.get("language"),
            recency_days=kwargs.get("recency_days"),
            categories=tuple(kwargs.get("categories") or ("general", "news")),
            domains=tuple(kwargs.get("domains") or ()),
            engines=tuple(kwargs.get("engines") or ()),
            excluded_domains=tuple(kwargs.get("excluded_domains") or ()),
            limit=kwargs.get("limit"),
        )
    text = _clean_text(query.text, limit=500)
    if not text:
        raise ValueError("search query must not be empty")
    if len(text) > 500:
        text = text[:500]
    try:
        recency = int(query.recency_days) if query.recency_days is not None else None
    except (TypeError, ValueError):
        recency = None
    recency = max(1, min(recency, 3_650)) if recency is not None else None
    categories = tuple(sorted({str(item).strip().lower() for item in query.categories if str(item).strip().lower() in _ALLOWED_CATEGORIES})) or ("general",)
    domains = tuple(dict.fromkeys(_clean_text(item, limit=120).lower().lstrip(".") for item in query.domains if _clean_text(item, limit=120)))[:8]
    engines = _normalize_engines(query.engines)
    excluded_domains = tuple(dict.fromkeys(_clean_text(item, limit=120).lower().lstrip(".") for item in query.excluded_domains if _clean_text(item, limit=120)))[:20]
    language = _clean_text(query.language, limit=32).lower() or None
    try:
        limit = int(query.limit) if query.limit is not None else max_results
    except (TypeError, ValueError):
        limit = max_results
    limit = max(1, min(limit, max_results))
    return SearchQuery(
        text=text,
        language=language,
        recency_days=recency,
        categories=categories,
        domains=domains,
        engines=engines,
        excluded_domains=excluded_domains,
        limit=limit,
    )


def _cache_key(query: SearchQuery) -> str:
    payload = json.dumps(
        {
            "q": query.text,
            "language": query.language,
            "recency_days": query.recency_days,
            "categories": query.categories,
            "domains": query.domains,
            "engines": query.engines,
            "excluded_domains": query.excluded_domains,
            "limit": query.limit,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DegradedSearchProvider:
    """Provider returned when search is disabled/missing configuration."""

    def __init__(self, *, provider: str = "searxng", message: str = "Web research is unavailable; search is degraded.") -> None:
        self.provider = provider
        self.message = message

    async def search(self, query: SearchQuery | str, **kwargs: Any) -> SearchResponse:
        options = dict(kwargs)
        options.pop("max_results", None)
        normalized = _normalize_query(query, max_results=max(1, int(kwargs.get("max_results", 10))), **options)
        return SearchResponse(query=normalized, provider=self.provider, degraded=True, warnings=(self.message,))


class SearXNGSearchProvider:
    """SearXNG JSON adapter with retries, cache, and normalized provenance."""

    provider = "searxng"

    def __init__(
        self,
        settings=None,
        *,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        retries: int | None = None,
        max_results: int | None = None,
        cache_ttl_seconds: float | None = None,
        cache: SearchCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._fixed_base_url = base_url is not None
        self.base_url = (base_url if base_url is not None else getattr(settings, "studio_search_base_url", "") or "").strip().rstrip("/")
        self.timeout_seconds = max(0.2, min(float(timeout_seconds if timeout_seconds is not None else getattr(settings, "studio_search_timeout_seconds", 10.0)), 30.0))
        self.retries = max(0, min(int(retries if retries is not None else getattr(settings, "studio_search_retries", 1)), 3))
        self.max_results = max(1, min(int(max_results if max_results is not None else getattr(settings, "studio_search_max_results", 10)), 50))
        self.max_queries = max(1, min(int(getattr(settings, "studio_search_max_queries", 3)), 8))
        self.engines = _normalize_engines(getattr(settings, "studio_search_engines", ""))
        self.allowed_engines = set(_normalize_engines(getattr(settings, "studio_search_allowed_engines", "")))
        self.blocked_domains = tuple(
            item.lower().lstrip(".")
            for item in str(getattr(settings, "studio_search_blocked_domains", "") or "").split(",")
            if item.strip()
        )
        self.cache = cache or SearchCache(ttl_seconds=float(cache_ttl_seconds if cache_ttl_seconds is not None else getattr(settings, "studio_search_cache_ttl_seconds", 900)))
        self.transport = transport
        self.client = client

    def _degraded(self, query: SearchQuery, message: str) -> SearchResponse:
        return SearchResponse(query=query, provider=self.provider, degraded=True, warnings=(message,))

    async def search(self, query: SearchQuery | str, **kwargs: Any) -> SearchResponse:
        # Settings are normally immutable for a process, but refreshing these
        # values makes test/reload configuration changes fail closed instead of
        # retaining a stale endpoint or bound.
        if self.settings is not None and not self._fixed_base_url:
            self.base_url = str(getattr(self.settings, "studio_search_base_url", self.base_url) or "").strip().rstrip("/")
            self.max_results = max(1, min(int(getattr(self.settings, "studio_search_max_results", self.max_results)), 50))
            self.retries = max(0, min(int(getattr(self.settings, "studio_search_retries", self.retries)), 3))
            self.engines = _normalize_engines(getattr(self.settings, "studio_search_engines", self.engines))
            self.allowed_engines = set(_normalize_engines(getattr(self.settings, "studio_search_allowed_engines", ",".join(self.allowed_engines))))
            self.blocked_domains = tuple(
                item.strip().lower().lstrip(".")
                for item in str(getattr(self.settings, "studio_search_blocked_domains", ",".join(self.blocked_domains)) or "").split(",")
                if item.strip()
            )
        options = dict(kwargs)
        options.pop("max_results", None)
        normalized = _normalize_query(query, max_results=self.max_results, max_queries=self.max_queries, **options)
        requested_engines = tuple(engine for engine in normalized.engines if not self.allowed_engines or engine in self.allowed_engines)
        rejected_engines = tuple(engine for engine in normalized.engines if self.allowed_engines and engine not in self.allowed_engines)
        if normalized.engines and not requested_engines:
            return self._degraded(normalized, "Requested engines are not enabled. Available engines: " + ", ".join(sorted(self.allowed_engines)))
        normalized = replace(normalized, engines=requested_engines or self.engines,
                             excluded_domains=tuple(dict.fromkeys([*self.blocked_domains, *normalized.excluded_domains])))
        state = configured_search_state(self.settings or type("Settings", (), {
            "studio_search_enabled": bool(self.base_url),
            "studio_search_provider": self.provider,
            "studio_search_base_url": self.base_url,
        })())
        if not state.enabled:
            return self._degraded(normalized, "Web research is disabled; enable private SearXNG to search.")
        if not state.configured or not self.base_url:
            return self._degraded(normalized, "Private SearXNG is not configured; search is degraded.")
        cached = await self.cache.get(_cache_key(normalized))
        if cached is not None:
            return cached
        fetched_at = _utcnow()
        params: dict[str, str] = {"q": normalized.text, "format": "json", "categories": ",".join(normalized.categories)}
        requested_engines = tuple(engine for engine in normalized.engines if not self.allowed_engines or engine in self.allowed_engines)
        selected_engines = requested_engines or self.engines
        if selected_engines:
            params["engines"] = ",".join(selected_engines)
        if normalized.language:
            params["language"] = normalized.language
        if normalized.recency_days is not None:
            params["time_range"] = "day" if normalized.recency_days <= 1 else "week" if normalized.recency_days <= 7 else "month" if normalized.recency_days <= 31 else "year"
        if normalized.domains:
            site_clause = " OR ".join(f"site:{domain}" for domain in normalized.domains)
            params["q"] = f'{normalized.text} ({site_clause})'
        attempts = self.retries + 1
        try:
            payload = await self._request(params, attempts=attempts)
            excluded = tuple(dict.fromkeys([*self.blocked_domains, *normalized.excluded_domains]))
            results, filtered_count, blocked_count = self._normalize_results(payload, normalized, fetched_at, excluded_domains=excluded)
            warnings_list = []
            if rejected_engines:
                warnings_list.append("Engines not enabled: " + ", ".join(rejected_engines))
            for failure in payload.get("unresponsive_engines", []) or []:
                if isinstance(failure, (list, tuple)) and failure:
                    engine = _clean_text(failure[0], limit=80)
                    reason = _clean_text(failure[1] if len(failure) > 1 else "unavailable", limit=120)
                    warnings_list.append(f"Engine {engine} did not respond successfully: {reason}.")
            if filtered_count:
                warnings_list.append(f"Filtered {filtered_count} low-relevance result{'s' if filtered_count != 1 else ''} for this precise query.")
            if blocked_count:
                warnings_list.append(f"Excluded {blocked_count} result{'s' if blocked_count != 1 else ''} from low-trust or explicitly excluded domains.")
            response = SearchResponse(
                query=normalized,
                results=tuple(results),
                provider=self.provider,
                fetched_at=fetched_at,
                degraded=bool(payload.get("unresponsive_engines")),
                warnings=tuple(warnings_list),
            )
            await self.cache.put(_cache_key(normalized), response)
            return response
        except SearchProviderError as exc:
            return self._degraded(normalized, exc.public_message)
        except (httpx.HTTPError, OSError, TimeoutError, ValueError, TypeError):
            return self._degraded(normalized, "Search provider failed; research is degraded.")

    async def _request(self, params: dict[str, str], *, attempts: int) -> dict[str, Any]:
        timeout = httpx.Timeout(self.timeout_seconds)
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=self.transport)
        try:
            last_error: SearchProviderError | None = None
            for attempt in range(max(1, attempts)):
                try:
                    response = await client.get(f"{self.base_url}/search", params=params)
                    if response.status_code in {408, 425, 429} or response.status_code >= 500:
                        raise SearchProviderError("search_provider_unavailable", "Search provider is temporarily unavailable; research is degraded.")
                    if response.status_code < 200 or response.status_code >= 300:
                        raise SearchProviderError("search_provider_rejected", "Search provider rejected the request; research is degraded.", retryable=False)
                    try:
                        payload = response.json()
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise SearchProviderError("search_invalid_response", "Search provider returned an invalid response; research is degraded.", retryable=False) from exc
                    if not isinstance(payload, dict):
                        raise SearchProviderError("search_invalid_response", "Search provider returned an invalid response; research is degraded.", retryable=False)
                    return payload
                except SearchProviderError as exc:
                    last_error = exc
                    if not exc.retryable or attempt + 1 >= max(1, attempts):
                        raise
                    await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
                except (httpx.TimeoutException, httpx.NetworkError, OSError, TimeoutError) as exc:
                    last_error = SearchProviderError("search_provider_timeout", "Search provider timed out; research is degraded.")
                    if attempt + 1 >= max(1, attempts):
                        raise last_error from exc
                    await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
            assert last_error is not None
            raise last_error
        finally:
            if own_client:
                await client.aclose()

    def _normalize_results(
        self,
        payload: dict[str, Any],
        query: SearchQuery,
        fetched_at: datetime,
        *,
        excluded_domains: tuple[str, ...] = (),
    ) -> tuple[list[SearchResult], int, int]:
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return [], 0, 0
        out: list[SearchResult] = []
        seen: set[str] = set()
        filtered_count = 0
        blocked_count = 0
        for index, raw in enumerate(raw_results):
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or raw.get("link") or "").strip()
            canonical = canonicalize_url(url)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            parsed = urlsplit(canonical)
            domain = (parsed.hostname or "").lower()
            if any(domain == blocked or domain.endswith(f".{blocked}") for blocked in excluded_domains):
                blocked_count += 1
                continue
            title = _clean_text(raw.get("title"), limit=500)
            snippet = _clean_text(raw.get("content") or raw.get("snippet") or raw.get("description"), limit=2_000)
            # Sanitize untrusted text and capture injection flags (local import to avoid circular)
            from .sources import sanitize_untrusted_text as _sanitize
            title, title_flags = _sanitize(title)
            snippet, snippet_flags = _sanitize(snippet)
            injection_flags = tuple(dict.fromkeys((*title_flags, *snippet_flags)))
            if not title and not snippet:
                continue
            source_name = _clean_text(raw.get("source") or raw.get("engine") or domain, limit=160) or domain
            published_at = _parse_datetime(raw.get("publishedDate") or raw.get("published") or raw.get("pubdate") or raw.get("date"))
            if published_at is None:
                published_at = _metadata_datetime(raw.get("metadata"), now=fetched_at)
            source_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
            relevance, matched_terms, term_count = _result_relevance(
                query=query.text,
                title=title,
                snippet=snippet,
                url=canonical,
            )
            # Lexical overlap is diagnostic, not a relevance gate. Paraphrases,
            # translations and niche terminology are for the agent to assess.
            out.append(
                SearchResult(
                    url=url,
                    canonical_url=canonical,
                    title=title,
                    snippet=snippet,
                    source_name=source_name,
                    domain=domain,
                    published_at=published_at,
                    provider=self.provider,
                    query=query.text,
                    fetched_at=fetched_at,
                    result_index=index,
                    source_id=source_id,
                    provenance={
                        "provider": self.provider,
                        "query": query.text,
                        "retrieved_at": _iso(fetched_at),
                        "result_index": index,
                        "date_available": published_at is not None,
                        "query_relevance": round(relevance, 4),
                        "matched_query_terms": matched_terms,
                        "query_term_count": term_count,
                        "required_concepts": [
                            name for name, query_pattern, _result_pattern in _QUERY_CONCEPTS if query_pattern.search(query.text)
                        ],
                        "injection_flags": list(injection_flags),
                    },
                    injection_flags=injection_flags,
                )
            )
            if len(out) >= (query.limit or self.max_results):
                break
        return out, filtered_count, blocked_count


def build_search_provider(settings, *, cache: SearchCache | None = None, transport=None) -> SearchProvider:
    """Return a provider selected by server configuration, never by the browser."""

    provider = str(getattr(settings, "studio_search_provider", "searxng") or "searxng").strip().lower()
    if provider != "searxng":
        return DegradedSearchProvider(provider=provider, message="The configured search provider is not supported in this deployment.")
    if not bool(getattr(settings, "studio_search_enabled", False)):
        return DegradedSearchProvider(message="Web research is disabled; enable private SearXNG to search.")
    return SearXNGSearchProvider(settings, cache=cache, transport=transport)


# Names used by early design notes and adapters remain available without
# creating a second provider abstraction.
SearchAdapter = SearchProvider
SearXNGSearchAdapter = SearXNGSearchProvider
