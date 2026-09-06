"""Workspace-scoped Content Studio domain and agent services."""

# Public M4 boundaries. Importing these modules has no network side effects.
from .provenance import ResearchBundle, SourceEvidence, StoryCluster
from .research import ResearchService
from .search import SearchQuery, SearchResponse, SearchResult, SearchProvider
from .sources import SafeSourceReader, SourceDocument

__all__ = [
    "ResearchBundle",
    "ResearchService",
    "SafeSourceReader",
    "SearchProvider",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
    "SourceDocument",
    "SourceEvidence",
    "StoryCluster",
]
