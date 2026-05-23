from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Iterable

from pydantic import ValidationError

from .dadata import DaDataClient
from .discovery import discover_leads
from .prompts import (
    COMBINED_PROMPT,
    EXTRACT_PROMPT,
    OKVED_PROMPT,
    PREFILTER_PROMPT,
    SCORE_PROMPT,
    SYSTEM_SAFE_SCRAPER,
    VALIDATE_CONTACT_PROMPT,
)
from .router import MultiProviderRouter
from .schemas import AIEnrichment, LeadRecord, PipelineStats, SearchCandidate
from .scraper import PublicScraper
from .storage import LeadStorage


@dataclass
class ParsedInputRow:
    company: str
    site: str
    city: str = ""
    niche: str = ""
    note: str = ""


def parse_manual_rows(text: str) -> list[ParsedInputRow]:
    rows: list[ParsedInputRow] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("компания;") or line.lower().startswith("company;"):
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2:
            continue
        rows.append(
            ParsedInputRow(
                company=parts[0],
                site=parts[1],
                city=parts[2] if len(parts) > 2 else "",
                niche=parts[3] if len(parts) > 3 else "",
                note=parts[4] if len(parts) > 4 else "",
            )
        )
    return rows


class LeadPipeline:
    """Pipeline orchestrator.

    Workflow:
      1. scrape company site (HTML, robots, throttle, SSRF guard)
      2. regex-extract emails/phones/telegram/social/INN/OGRN/KPP
      3. (optional) DaData enrichment by INN/OGRN/site
      4. (optional) prefilter — cheap model says "no contacts here, skip"
      5. AI enrichment:
         - combined mode: ONE call covering validate+extract+score
         - legacy mode: 3 sequential calls
      6. dedupe by INN/OGRN/site
      7. persist to SQLite
    """

    def __init__(
        self,
        router: MultiProviderRouter | None = None,
        scraper: PublicScraper | None = None,
        storage: LeadStorage | None = None,
        dadata: DaDataClient | None = None,
        use_combined_prompt: bool = True,
        use_prefilter: bool = False,
    ):
        self.router = router or MultiProviderRouter()
        self.scraper = scraper or PublicScraper()
        self.storage = storage or LeadStorage()
        self.dadata = dadata or DaDataClient()
        self.use_combined_prompt = use_combined_prompt
        self.use_prefilter = use_prefilter

    async def discover(
        self,
        query: str,
        city: str = "",
        niche: str = "",
        seed_urls: list[str] | None = None,
        limit: int = 20,
    ) -> list[SearchCandidate]:
        return await discover_leads(query=query, city=city, niche=niche, seed_urls=seed_urls, limit=limit)

    def _is_duplicate(self, lead: LeadRecord) -> tuple[bool, str]:
        """Возвращает (is_dup, key_used). Дедуп по ИНН → ОГРН → site."""
        if lead.inn:
            found = self.storage.find_by_inn(lead.inn)
            if found and found.site != lead.site:
                return True, f"inn={lead.inn}"
        if lead.ogrn:
            found = self.storage.find_by_ogrn(lead.ogrn)
            if found and found.site != lead.site:
                return True, f"ogrn={lead.ogrn}"
        return False, ""

    async def process_candidate(self, row: ParsedInputRow | SearchCandidate) -> LeadRecord:
        from urllib.parse import urlparse

        if isinstance(row, SearchCandidate):
            company = row.title or urlparse(row.url).netloc.removeprefix("www.")
            site, city, niche, note = row.url, row.city, row.niche, row.snippet
        else:
            company, site, city, niche, note = row.company, row.site, row.city, row.niche, row.note

        contacts, page_text = await self.scraper.scrape_company_site(site)
        contacts.company = company

        lead = LeadRecord(
            company=company,
            site=contacts.site or site,
            city=city,
            niche=niche,
            note=note,
            phones=contacts.phones,
            emails=contacts.emails,
            telegram=contacts.telegram,
            social_links=contacts.social_links,
            contact_page=contacts.contact_page,
            source_url=contacts.source_url or site,
            inn=contacts.inn,
            ogrn=contacts.ogrn,
            kpp=contacts.kpp,
            collected_at=contacts.collected_at,
            status="parsed",
            limitations=contacts.limitations,
        )

        # DaData enrichment (опционально, требует DADATA_API_KEY)
        if self.dadata.is_configured:
            dd = await self.dadata.enrich(inn=lead.inn, ogrn=lead.ogrn, site=lead.site)
            if dd:
                lead.legal_name = dd.get("legal_name") or lead.legal_name
                lead.legal_address = dd.get("address") or lead.legal_address
                lead.inn = lead.inn or dd.get("inn")
                lead.ogrn = lead.ogrn or dd.get("ogrn")
                lead.kpp = lead.kpp or dd.get("kpp")

        # Если контактов в принципе нет и текст пуст — не тратим LLM-токены.
        if not page_text and not (contacts.emails or contacts.phones or contacts.telegram):
            lead.enrichment = self._fallback_enrichment(lead, "no_page_text")
            lead.status = "needs_review"
            return lead

        # Прифильтр (опциональный, capability='prefilter')
        if self.use_prefilter:
            pre = await self.router.call_json(
                "prefilter",
                PREFILTER_PROMPT.format(text=page_text[:6000]),
                system=SYSTEM_SAFE_SCRAPER,
                max_tokens=200,
                allow_cache=True,
            )
            if not pre.get("error") and pre.get("has_contacts") is False:
                lead.limitations.append(f"prefilter_skip: {pre.get('reason', '')[:120]}")

        if self.use_combined_prompt:
            await self._enrich_combined(lead, page_text)
        else:
            await self._enrich_sequential(lead, contacts, page_text)

        if lead.enrichment.first_message:
            lead.status = "draft_ready"

        # Помечаем дубли — но всё равно возвращаем (UI решит, что делать).
        is_dup, key = self._is_duplicate(lead)
        if is_dup:
            lead.limitations.append(f"duplicate_by_{key}")
            lead.status = "needs_review"
        return lead

    async def _enrich_combined(self, lead: LeadRecord, page_text: str) -> None:
        """ОДИН LLM-вызов вместо трёх. capability='extract' (универсальная)."""
        prompt = COMBINED_PROMPT.format(
            company=lead.company,
            site=lead.site,
            city=lead.city,
            niche=lead.niche,
            note=lead.note,
            contacts_json=json.dumps(lead.model_dump(mode="json"), ensure_ascii=False),
            text=page_text[:14000],
        )
        result = await self.router.call_json(
            "extract",
            prompt,
            system=SYSTEM_SAFE_SCRAPER,
            max_tokens=1400,
        )
        if result.get("error"):
            lead.enrichment = self._fallback_enrichment(lead, result.get("details", "AI extraction failed"))
            lead.status = "needs_review"
            return
        # Validation block (часть combined ответа)
        validation = result.get("validation") or {}
        if isinstance(validation, dict):
            valid_emails = validation.get("valid_emails")
            valid_phones = validation.get("valid_phones")
            valid_telegram = validation.get("valid_telegram")
            is_company = validation.get("is_company_contact")
            # BUG-2 fix: применяем валидацию ПРАВИЛЬНО
            if is_company is False:
                lead.emails, lead.phones, lead.telegram = [], [], []
                lead.limitations.append("contacts_belong_to_third_party")
            else:
                if isinstance(valid_emails, list):
                    lead.emails = [e for e in lead.emails if e in valid_emails]
                if isinstance(valid_phones, list):
                    lead.phones = [p for p in lead.phones if p in valid_phones]
                if isinstance(valid_telegram, list):
                    lead.telegram = [t for t in lead.telegram if t in valid_telegram]
            lead.limitations.extend(validation.get("risks", []) or [])
        # Enrichment block
        enrichment_payload = result.get("enrichment") or result
        try:
            lead.enrichment = AIEnrichment(**{k: v for k, v in enrichment_payload.items() if k in AIEnrichment.model_fields})
            lead.status = "enriched"
        except ValidationError as exc:
            lead.enrichment = self._fallback_enrichment(lead, str(exc))
            lead.status = "needs_review"

    async def _enrich_sequential(self, lead: LeadRecord, contacts, page_text: str) -> None:
        """Старый путь: 3 отдельных вызова. Оставлен для совместимости."""
        contacts_json = json.dumps(contacts.model_dump(mode="json"), ensure_ascii=False)
        if contacts.emails or contacts.phones or contacts.telegram:
            validation = await self.router.call_json(
                "validate",
                VALIDATE_CONTACT_PROMPT.format(
                    company=lead.company, site=lead.site, contacts_json=contacts_json, text=page_text[:6000]
                ),
                system=SYSTEM_SAFE_SCRAPER,
                max_tokens=700,
            )
            if not validation.get("error"):
                # BUG-2 fix
                if validation.get("is_company_contact") is False:
                    lead.emails, lead.phones, lead.telegram = [], [], []
                    lead.limitations.append("contacts_belong_to_third_party")
                else:
                    ve = validation.get("valid_emails")
                    vp = validation.get("valid_phones")
                    vt = validation.get("valid_telegram")
                    if isinstance(ve, list):
                        lead.emails = [e for e in lead.emails if e in ve]
                    if isinstance(vp, list):
                        lead.phones = [p for p in lead.phones if p in vp]
                    if isinstance(vt, list):
                        lead.telegram = [t for t in lead.telegram if t in vt]
                lead.limitations.extend(validation.get("risks", []) or [])

        extraction = await self.router.call_json(
            "extract",
            EXTRACT_PROMPT.format(
                company=lead.company,
                site=lead.site,
                city=lead.city,
                niche=lead.niche,
                note=lead.note,
                contacts_json=json.dumps(lead.model_dump(mode="json"), ensure_ascii=False),
                text=page_text[:18000],
            ),
            system=SYSTEM_SAFE_SCRAPER,
            max_tokens=1200,
        )
        if extraction.get("error"):
            lead.enrichment = self._fallback_enrichment(lead, extraction.get("details", "AI extraction failed"))
            lead.status = "needs_review"
        else:
            try:
                lead.enrichment = AIEnrichment(**{k: v for k, v in extraction.items() if k in AIEnrichment.model_fields})
                lead.status = "enriched"
            except ValidationError as exc:
                lead.enrichment = self._fallback_enrichment(lead, str(exc))
                lead.status = "needs_review"

        score_payload = await self.router.call_json(
            "score",
            SCORE_PROMPT.format(lead_json=json.dumps(lead.model_dump(mode="json"), ensure_ascii=False)),
            system=SYSTEM_SAFE_SCRAPER,
            max_tokens=500,
        )
        if not score_payload.get("error"):
            try:
                if "score" in score_payload:
                    lead.enrichment.score = int(score_payload.get("score", lead.enrichment.score))
                if score_payload.get("priority"):
                    lead.enrichment.priority = score_payload["priority"]
                if score_payload.get("reasoning_short"):
                    lead.enrichment.reasoning_short = score_payload["reasoning_short"]
                if score_payload.get("risks"):
                    lead.enrichment.risks.extend(score_payload.get("risks", []))
            except Exception:
                pass

    async def process_many(
        self,
        rows: Iterable[ParsedInputRow | SearchCandidate],
        save: bool = True,
        concurrency: int = 3,
    ) -> tuple[list[LeadRecord], PipelineStats]:
        stats = PipelineStats()
        sem = asyncio.Semaphore(concurrency)
        leads: list[LeadRecord] = []
        stats.discovered = len(list(rows)) if hasattr(rows, "__len__") else 0

        # rebuild iterator (если использовали len)
        rows_list = list(rows)
        stats.discovered = len(rows_list)

        async def run_one(row):
            async with sem:
                try:
                    lead = await self.process_candidate(row)
                    if save:
                        self.storage.upsert(lead)
                        stats.saved += 1
                    stats.parsed += 1
                    if lead.status in {"enriched", "draft_ready"}:
                        stats.enriched += 1
                    if "duplicate_by_" in " ".join(lead.limitations):
                        stats.skipped += 1
                    return lead
                except Exception as exc:
                    stats.errors.append(str(exc)[:200])
                    stats.skipped += 1
                    return None

        tasks = [run_one(row) for row in rows_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                stats.errors.append(str(result)[:200])
                continue
            if result is not None:
                leads.append(result)
        return leads, stats

    def _fallback_enrichment(self, lead: LeadRecord, reason: str) -> AIEnrichment:
        channel = "email" if lead.emails else ("telegram" if lead.telegram else ("phone" if lead.phones else "manual"))
        tag = "не определено"
        niche_l = (lead.niche or "").lower()
        if "магаз" in niche_l or "ecom" in niche_l or "интернет" in niche_l:
            tag = "интернет-магазин"
        elif "логист" in niche_l:
            tag = "логистика"
        elif "производ" in niche_l:
            tag = "производитель"
        return AIEnrichment(
            lead_tag=tag,
            decision_maker_role="собственник или руководитель продаж",
            pain="нужна ручная проверка: AI-модель не вернула валидный JSON",
            utp="предложить короткий аудит процессов продаж и клиентских обращений",
            first_message=(
                f"Добрый день. Нашёл сайт {lead.company}. Можем быстро посмотреть, "
                "где AI-автоматизация снимет ручную обработку заявок и типовых вопросов. "
                "Актуально обсудить короткий аудит?"
            ),
            best_channel=channel,
            priority="unknown",
            score=0,
            reasoning_short=reason[:300],
            risks=["fallback_enrichment"],
        )


def run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    else:
        return loop.run_until_complete(coro)
