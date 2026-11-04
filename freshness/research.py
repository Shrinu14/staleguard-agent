"""
Step 4 of the freshness pipeline: live research with Tavily.

  - Searches are restricted to the library's official doc domains
    (lookups.LIBRARY_DOC_DOMAINS; DEFAULT_DOC_DOMAINS otherwise), so the
    judge only sees trustworthy sources.
  - The query is built ONLY from names: distribution, version, API symbol.
    Every term must match SAFE_QUERY_TERM_PATTERN (identifiers, dots,
    version specifiers), so source code can never be sent — not by a bug,
    and not in a confidential repo. A term that doesn't match raises.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from agent import lookups
from freshness.extract import ApiUse
from freshness.versions import VersionInfo

_SAFE = re.compile(lookups.SAFE_QUERY_TERM_PATTERN)


class ResearchError(RuntimeError):
    """Tavily is not configured or the search failed."""


@dataclass(frozen=True, slots=True)
class Source:
    url: str
    title: str
    content: str  # Tavily's extracted snippet, clipped


class Searcher(Protocol):
    def __call__(self, use: ApiUse, version: VersionInfo) -> list[Source]: ...


def doc_domains(library: str) -> tuple[str, ...]:
    return lookups.LIBRARY_DOC_DOMAINS.get(library, lookups.DEFAULT_DOC_DOMAINS)


def build_query(use: ApiUse, version: VersionInfo) -> str:
    terms = {"distribution": version.distribution, "symbol": use.symbol,
             "version": version.version or ""}
    for name, value in terms.items():
        if value and not _SAFE.fullmatch(value):
            raise ResearchError(f"refusing to search: {name} {value!r} is not a plain name")
    return " ".join(lookups.FRESHNESS_QUERY_TEMPLATE.format(**terms).split())


class TavilySearcher:
    """Searcher backed by the Tavily API (tavily-python)."""

    def __init__(self, api_key: str, *, client_factory: Callable[[str], Any] | None = None):
        if not api_key:
            raise ResearchError("TAVILY_API_KEY is not set in .env.")
        if client_factory is None:
            from tavily import TavilyClient
            client_factory = TavilyClient
        self._client = client_factory(api_key)

    def __call__(self, use: ApiUse, version: VersionInfo) -> list[Source]:
        query = build_query(use, version)
        try:
            raw = self._client.search(
                query,
                search_depth=lookups.TAVILY_SEARCH_DEPTH,
                max_results=lookups.TAVILY_MAX_RESULTS,
                include_domains=list(doc_domains(use.library)),
            )
        except Exception as exc:  # network, quota, auth: report, never crash the edit
            raise ResearchError(f"Tavily search failed: {type(exc).__name__}: {exc}") from exc
        return [
            Source(str(r.get("url", "")), str(r.get("title", "")),
                   str(r.get("content", ""))[:lookups.FRESHNESS_SNIPPET_CHARS])
            for r in raw.get("results", []) if r.get("url")
        ]
