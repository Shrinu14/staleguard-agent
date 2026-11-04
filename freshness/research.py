"""
Step 4 of the freshness pipeline: live research with Tavily.

  - Searches are restricted to the library's official doc domains
    (lookups.LIBRARY_DOC_DOMAINS; DEFAULT_DOC_DOMAINS otherwise), so the
    judge only sees trustworthy sources.
  - The query is built ONLY from names: distribution, version, API symbol.
    Every term must match SAFE_QUERY_TERM_PATTERN (identifiers, dots,
    version specifiers), so source code can never be sent — not by a bug,
    and not in a confidential repo. A term that doesn't match raises.
  - `staleguard verify` also searches prose claims (search_claim). That text
    is the answer the user asked us to check, not repo code; it is cut to one
    line of VERIFY_QUERY_MAX_CHARS. Known library topics stay restricted to
    their doc domains; other topics search the open web.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from agent import lookups
from freshness.claims import ProseClaim
from freshness.extract import ApiUse
from freshness.textutil import normalize
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


class ClaimSearcher(Protocol):
    def search_claim(self, claim: ProseClaim) -> list[Source]: ...


def is_official(url: str, library: str) -> bool:
    """True when url is on a documentation domain listed for `library`.
    Unknown libraries/topics have no official domain, so never count."""
    host = url.split("://", 1)[-1].split("/", 1)[0].lower()
    domains = lookups.LIBRARY_DOC_DOMAINS.get(library, ())
    return any(host == d or host.endswith("." + d) for d in domains)


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
        return self._search(build_query(use, version), list(doc_domains(use.library)))

    def search_claim(self, claim: ProseClaim) -> list[Source]:
        # The claim sentence carries the specific terms (`@field_validator`, "3.8");
        # a model-written query tends to be vaguer ("pydantic v2 deprecations").
        text = claim.claim or claim.search_query
        query = " ".join(text.split())[:lookups.VERIFY_QUERY_MAX_CHARS]
        if not query:
            raise ResearchError("empty search query")
        domains = lookups.LIBRARY_DOC_DOMAINS.get(claim.topic)
        return self._search(query, list(domains) if domains else None)

    def _search(self, query: str, domains: list[str] | None) -> list[Source]:
        kwargs: dict[str, Any] = {"search_depth": lookups.TAVILY_SEARCH_DEPTH,
                                  "max_results": lookups.TAVILY_MAX_RESULTS}
        if domains:
            kwargs["include_domains"] = domains
        try:
            raw = self._client.search(query, **kwargs)
        except Exception as exc:  # network, quota, auth: report, never crash the edit
            raise ResearchError(f"Tavily search failed: {type(exc).__name__}: {exc}") from exc
        return dedupe([
            Source(str(r.get("url", "")), str(r.get("title", "")),
                   str(r.get("content", ""))[:lookups.FRESHNESS_SNIPPET_CHARS])
            for r in raw.get("results", []) if r.get("url")
        ])


def dedupe(sources: list[Source]) -> list[Source]:
    """Drop repeats: same page (ignoring #anchors) or same snippet text.
    Tavily often returns one page under several anchors with identical content."""
    seen_urls: set[str] = set()
    seen_text: set[str] = set()
    out = []
    for src in sources:
        url = src.url.split("#", 1)[0].rstrip("/")
        text = normalize(src.content)
        if url in seen_urls or (text and text in seen_text):
            continue
        seen_urls.add(url)
        seen_text.add(text)
        out.append(src)
    return out
