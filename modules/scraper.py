from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
import urllib.robotparser
from dataclasses import dataclass, field
from html import unescape
from typing import Iterable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .schemas import (
    ContactExtraction,
    EMAIL_RE,
    INN_RE,
    KPP_RE,
    OGRN_RE,
    PHONE_RE,
    TG_RE,
)

CONTACT_HINTS = (
    "contact",
    "contacts",
    "kontakty",
    "kontakt",
    "about",
    "company",
    "o-kompanii",
    "o-nas",
    "feedback",
    "rekvizity",
    "requisites",
    "info",
    "imprint",
)
SOCIAL_HOSTS = (
    "vk.com",
    "t.me",
    "telegram.me",
    "youtube.com",
    "rutube.ru",
    "dzen.ru",
    "ok.ru",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
)

USER_AGENT = "ContactScraperBot/1.1 (+public-data-compliance; manual-review)"

# Лимит размера ответа (10 MB) — защита от OOM.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# Дополнительный flag: разрешать ли частные IP (для локального dev).
ALLOW_PRIVATE_IPS_ENV = "SCRAPER_ALLOW_PRIVATE_IPS"


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    html: str
    text: str
    error: str = ""


def _hostname_is_private(host: str) -> bool:
    """Проверяет, разрешается ли хост в private/loopback/link-local IP.
    Защита от SSRF: blocks 127.x, 10.x, 192.168.x, 169.254.x, IPv6 ::1 и т.п.
    """
    if not host:
        return True
    # Иногда host = '[::1]'
    host = host.strip("[]")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # неразрешимый хост — не пускаем
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return True
    return False


@dataclass
class PublicScraper:
    max_pages_per_site: int = 4
    timeout_sec: int = 20
    per_domain_delay_sec: float = 1.0
    respect_robots: bool = True
    user_agent: str = USER_AGENT
    allow_private_ips: bool = False
    _last_access: dict[str, float] = field(default_factory=dict)
    _robots_cache: dict[str, urllib.robotparser.RobotFileParser] = field(default_factory=dict)
    _domain_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def normalize_url(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            raise ValueError("Empty URL")
        if not re.match(r"^https?://", url, flags=re.I):
            url = "https://" + url
        return url

    def same_domain(self, a: str, b: str) -> bool:
        return urlparse(a).netloc.replace("www.", "") == urlparse(b).netloc.replace("www.", "")

    def _is_ssrf_target(self, url: str) -> bool:
        """SSRF guard. Возвращает True, если URL нельзя фетчить."""
        import os
        if self.allow_private_ips or os.getenv(ALLOW_PRIVATE_IPS_ENV, "").lower() in {"1", "true", "yes"}:
            return False
        try:
            parsed = urlparse(url)
        except Exception:
            return True
        if parsed.scheme not in {"http", "https"}:
            return True
        if not parsed.hostname:
            return True
        # Прямой IP в URL
        try:
            ip = ipaddress.ip_address(parsed.hostname)
            return (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            )
        except ValueError:
            pass
        # Иначе резолвим имя
        return _hostname_is_private(parsed.hostname)

    async def robots_allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots_cache:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = urljoin(base, "/robots.txt")
            try:
                async with httpx.AsyncClient(
                    timeout=8, follow_redirects=True, headers={"User-Agent": self.user_agent}
                ) as client:
                    resp = await client.get(robots_url)
                if resp.status_code < 400:
                    rp.parse(resp.text.splitlines())
                else:
                    rp.parse([])
            except Exception:
                rp.parse([])
            self._robots_cache[base] = rp
        return self._robots_cache[base].can_fetch(self.user_agent, url)

    def _domain_lock(self, url: str) -> asyncio.Lock:
        domain = urlparse(url).netloc
        if domain not in self._domain_locks:
            self._domain_locks[domain] = asyncio.Lock()
        return self._domain_locks[domain]

    async def _throttle(self, url: str) -> None:
        domain = urlparse(url).netloc
        now = time.time()
        last = self._last_access.get(domain, 0)
        wait = self.per_domain_delay_sec - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_access[domain] = time.time()

    async def fetch(self, url: str) -> FetchResult:
        try:
            url = self.normalize_url(url)
        except Exception as exc:
            return FetchResult(url=url or "", final_url=url or "", status_code=0, html="", text="", error=f"bad_url: {exc}")

        # SSRF-guard
        if self._is_ssrf_target(url):
            return FetchResult(url=url, final_url=url, status_code=0, html="", text="", error="ssrf_blocked")

        if not await self.robots_allowed(url):
            return FetchResult(url=url, final_url=url, status_code=0, html="", text="", error="blocked_by_robots_txt")

        # Per-domain lock + throttle (последовательный доступ в пределах одного хоста)
        lock = self._domain_lock(url)
        async with lock:
            await self._throttle(url)
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_sec,
                    follow_redirects=True,
                    headers={"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"},
                    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
                ) as client:
                    resp = await client.get(url)
                ctype = resp.headers.get("content-type", "")
                # Проверяем content-length до чтения, если есть
                clen = resp.headers.get("content-length")
                if clen and clen.isdigit() and int(clen) > MAX_RESPONSE_BYTES:
                    return FetchResult(url=url, final_url=str(resp.url), status_code=resp.status_code, html="", text="", error="too_large")
                is_html = (
                    "text/html" in ctype
                    or "application/xhtml" in ctype
                    or resp.text.strip().startswith("<")
                )
                if not is_html:
                    return FetchResult(url=url, final_url=str(resp.url), status_code=resp.status_code, html="", text="", error="not_html")
                # Усечение слишком большого html
                html = resp.text[:MAX_RESPONSE_BYTES]
                text = self.html_to_text(html)
                return FetchResult(url=url, final_url=str(resp.url), status_code=resp.status_code, html=html, text=text)
            except Exception as exc:
                return FetchResult(url=url, final_url=url, status_code=0, html="", text="", error=str(exc)[:200])

    def html_to_text(self, html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        text = soup.get_text(" ")
        text = unescape(re.sub(r"\s+", " ", text)).strip()
        return text[:80_000]

    def extract_links(self, base_url: str, html: str) -> list[str]:
        soup = BeautifulSoup(html or "", "html.parser")
        links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = str(a.get("href", "")).strip()
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            full = urljoin(base_url, href)
            if self.same_domain(base_url, full) and full not in links:
                links.append(full)
        return links

    def contact_links(self, base_url: str, html: str) -> list[str]:
        links = self.extract_links(base_url, html)
        contact_like = []
        for link in links:
            path = (urlparse(link).path or "").lower()
            if any(h in path for h in CONTACT_HINTS):
                contact_like.append(link)
        return contact_like[: self.max_pages_per_site - 1]

    def extract_contacts_from_html(self, url: str, html: str, text: str = "") -> ContactExtraction:
        """Извлечение контактов из HTML и чистого текста.
        BUG-8 fix: e-mail и телефоны ищем в **тексте** (без HTML-шума) + явно в href.
        """
        soup = BeautifulSoup(html or "", "html.parser")
        clean_text = text or self.html_to_text(html)

        # Только текст (без HTML-разметки), плюс отдельно href-источники.
        emails = list(EMAIL_RE.findall(clean_text))
        phones = list(PHONE_RE.findall(clean_text))
        telegram = list(TG_RE.findall(clean_text))
        social_links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = str(a.get("href", "")).strip()
            full = urljoin(url, href)
            host = urlparse(full).netloc.lower()
            if any(social in host for social in SOCIAL_HOSTS) and full not in social_links:
                social_links.append(full)
            if href.startswith("mailto:"):
                emails.append(href[len("mailto:"):].split("?")[0])
            if href.startswith("tel:"):
                phones.append(href[len("tel:"):])
        # ИНН/ОГРН/КПП — только в чистом тексте.
        inns = INN_RE.findall(clean_text)
        ogrns = OGRN_RE.findall(clean_text)
        kpps = KPP_RE.findall(clean_text)
        return ContactExtraction(
            site=url,
            source_url=url,
            emails=emails,
            phones=phones,
            telegram=telegram,
            social_links=social_links,
            inn=inns[0] if inns else None,
            ogrn=ogrns[0] if ogrns else None,
            kpp=kpps[0] if kpps else None,
        )

    async def scrape_company_site(self, site: str) -> tuple[ContactExtraction, str]:
        """Return merged contacts and compact page text for AI analysis."""
        site = self.normalize_url(site)
        first = await self.fetch(site)
        limitations: list[str] = []
        if first.error:
            limitations.append(first.error)
            return ContactExtraction(site=site, source_url=site, limitations=limitations), ""
        pages: list[FetchResult] = [first]
        for link in self.contact_links(first.final_url, first.html):
            if len(pages) >= self.max_pages_per_site:
                break
            page = await self.fetch(link)
            pages.append(page)
            if page.error:
                limitations.append(f"{link}: {page.error}")
        merged = ContactExtraction(site=first.final_url, source_url=site, limitations=limitations)
        texts: list[str] = []
        for page in pages:
            if page.html:
                ce = self.extract_contacts_from_html(page.final_url, page.html, page.text)
                for attr in ["emails", "phones", "telegram", "social_links"]:
                    for item in getattr(ce, attr):
                        if item not in getattr(merged, attr):
                            getattr(merged, attr).append(item)
                # Сохраняем первую найденную тройку ИНН/ОГРН/КПП.
                if ce.inn and not merged.inn:
                    merged.inn = ce.inn
                if ce.ogrn and not merged.ogrn:
                    merged.ogrn = ce.ogrn
                if ce.kpp and not merged.kpp:
                    merged.kpp = ce.kpp
                if any(h in (urlparse(page.final_url).path or "").lower() for h in CONTACT_HINTS):
                    merged.contact_page = page.final_url
                texts.append(f"URL: {page.final_url}\n{page.text[:12000]}")
        return merged, "\n\n".join(texts)[:50_000]

    async def discover_links_from_seed_pages(self, seed_urls: Iterable[str], limit: int = 50) -> list[str]:
        found: list[str] = []
        for seed in seed_urls:
            if len(found) >= limit:
                break
            result = await self.fetch(seed)
            if result.error or not result.html:
                continue
            for link in self.extract_links(result.final_url, result.html):
                if link not in found:
                    found.append(link)
                if len(found) >= limit:
                    break
        return found
