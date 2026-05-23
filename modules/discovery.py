from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx

from .schemas import SearchCandidate
from .scraper import PublicScraper


def _domain_root(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else url


async def search_web(query: str, city: str = "", niche: str = "", limit: int = 20) -> list[SearchCandidate]:
    """Search via optional APIs. Without keys returns empty list."""
    full_query = " ".join(x for x in [query, city, niche] if x).strip()
    results: list[SearchCandidate] = []
    if not full_query:
        return results

    brave = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if brave and len(results) < limit:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    headers={"X-Subscription-Token": brave, "Accept": "application/json"},
                    params={"q": full_query, "count": min(limit, 20), "safesearch": "moderate"},
                )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("web", {}).get("results", []):
                results.append(SearchCandidate(title=item.get("title", ""), url=_domain_root(item.get("url", "")), snippet=item.get("description", ""), source="brave", city=city, niche=niche))
        except Exception:
            pass

    serper = os.getenv("SERPER_API_KEY", "").strip()
    if serper and len(results) < limit:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": serper, "Content-Type": "application/json"},
                    json={"q": full_query, "num": min(limit, 20)},
                )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("organic", []):
                results.append(SearchCandidate(title=item.get("title", ""), url=_domain_root(item.get("link", "")), snippet=item.get("snippet", ""), source="serper", city=city, niche=niche))
        except Exception:
            pass

    tavily = os.getenv("TAVILY_API_KEY", "").strip()
    if tavily and len(results) < limit:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": tavily, "query": full_query, "max_results": min(limit, 20), "search_depth": "basic"},
                )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("results", []):
                results.append(SearchCandidate(title=item.get("title", ""), url=_domain_root(item.get("url", "")), snippet=item.get("content", ""), source="tavily", city=city, niche=niche))
        except Exception:
            pass

    # de-duplicate by domain root
    deduped: list[SearchCandidate] = []
    seen = set()
    for item in results:
        if item.url not in seen:
            deduped.append(item)
            seen.add(item.url)
        if len(deduped) >= limit:
            break
    return deduped


async def discover_leads(
    query: str,
    city: str = "",
    niche: str = "",
    seed_urls: list[str] | None = None,
    limit: int = 20,
) -> list[SearchCandidate]:
    candidates = await search_web(query=query, city=city, niche=niche, limit=limit)
    if len(candidates) < limit and seed_urls:
        scraper = PublicScraper(max_pages_per_site=1)
        links = await scraper.discover_links_from_seed_pages(seed_urls, limit=limit - len(candidates))
        for link in links:
            try:
                candidates.append(SearchCandidate(title=urlparse(link).netloc, url=_domain_root(link), snippet="найдено на seed-странице", source="seed_page", city=city, niche=niche))
            except Exception:
                continue
    # final dedupe
    out: list[SearchCandidate] = []
    seen = set()
    for c in candidates:
        if c.url not in seen:
            out.append(c)
            seen.add(c.url)
        if len(out) >= limit:
            break
    return out
