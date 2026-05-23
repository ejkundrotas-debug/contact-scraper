"""DaData API client for company enrichment by INN/OGRN/site.

Docs: https://dadata.ru/api/suggest/party/
Free tier: 10,000 suggestions/day, register at https://dadata.ru/
"""
from __future__ import annotations

import os
from typing import Any

import httpx

DADATA_API_KEY_ENV = "DADATA_API_KEY"
DADATA_BASE = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"


class DaDataClient:
    """Async client for DaData (party suggest + findById)."""

    def __init__(self, api_key: str | None = None, timeout: int = 15):
        self.api_key = (api_key or os.getenv(DADATA_API_KEY_ENV, "")).strip()
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Token {self.api_key}",
        }

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if not self.is_configured:
            return None
        url = f"{DADATA_BASE}/{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=body)
            if resp.status_code >= 400:
                return None
            return resp.json()
        except Exception:
            return None

    @staticmethod
    def _normalize_party(suggestion: dict[str, Any]) -> dict[str, Any]:
        """Превращает DaData-ответ в плоскую структуру."""
        data = suggestion.get("data", {}) or {}
        name = data.get("name", {}) or {}
        address = data.get("address", {}) or {}
        management = data.get("management", {}) or {}
        return {
            "legal_name": name.get("full_with_opf") or name.get("short_with_opf") or suggestion.get("value"),
            "short_name": name.get("short_with_opf"),
            "inn": data.get("inn"),
            "kpp": data.get("kpp"),
            "ogrn": data.get("ogrn"),
            "okved": data.get("okved"),
            "okved_type": data.get("okved_type"),
            "address": address.get("value") or address.get("unrestricted_value"),
            "address_data": address.get("data"),
            "manager_name": management.get("name"),
            "manager_position": management.get("post"),
            "status": (data.get("state") or {}).get("status"),
            "registration_date": (data.get("state") or {}).get("registration_date"),
            "actuality_date": (data.get("state") or {}).get("actuality_date"),
            "branch_type": data.get("branch_type"),
            "type": data.get("type"),  # LEGAL | INDIVIDUAL
        }

    async def find_by_inn(self, inn: str) -> dict[str, Any] | None:
        if not inn:
            return None
        data = await self._post("findById/party", {"query": inn})
        if not data:
            return None
        suggestions = data.get("suggestions") or []
        if not suggestions:
            return None
        return self._normalize_party(suggestions[0])

    async def suggest_by_name(self, name: str, count: int = 5) -> list[dict[str, Any]]:
        if not name:
            return []
        data = await self._post("suggest/party", {"query": name, "count": count})
        if not data:
            return []
        return [self._normalize_party(s) for s in data.get("suggestions", [])]

    async def find_by_site(self, site: str) -> dict[str, Any] | None:
        """DaData не ищет по сайту напрямую, но иногда сайт = бренд.
        Этот метод — best-effort: чистим домен, ищем как название.
        """
        if not site:
            return None
        from urllib.parse import urlparse

        netloc = urlparse(site).netloc or site
        brand = netloc.removeprefix("www.").split(".")[0]
        if len(brand) < 3:
            return None
        suggestions = await self.suggest_by_name(brand, count=1)
        return suggestions[0] if suggestions else None

    async def enrich(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        site: str | None = None,
    ) -> dict[str, Any] | None:
        """Best-effort: пробуем по ИНН, потом по ОГРН (как ИНН-параметру), потом по сайту."""
        if inn:
            res = await self.find_by_inn(inn)
            if res:
                return res
        if ogrn:
            res = await self.find_by_inn(ogrn)  # API findById принимает и ИНН и ОГРН
            if res:
                return res
        if site:
            return await self.find_by_site(site)
        return None
