"""CRM integrations.

Supported (lightweight, webhook-style):
- Bitrix24 (incoming webhook URL)
- amoCRM (long-lived API token)
- Generic webhook (любой URL — POST JSON, для Shop-logistics fulfillment, n8n, Make, Zapier)
- HubSpot (private app token)

Все методы async, возвращают {"ok": bool, "id"|"error": ...}.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from .schemas import LeadRecord


def _lead_to_generic_payload(lead: LeadRecord) -> dict[str, Any]:
    """Универсальная плоская структура для большинства webhook-приёмников."""
    enr = lead.enrichment
    return {
        "company": lead.company,
        "site": lead.site,
        "city": lead.city,
        "niche": lead.niche,
        "inn": lead.inn,
        "ogrn": lead.ogrn,
        "kpp": lead.kpp,
        "legal_name": lead.legal_name,
        "legal_address": lead.legal_address,
        "contact": lead.contact,
        "emails": lead.emails,
        "phones": lead.phones,
        "telegram": lead.telegram,
        "social_links": lead.social_links,
        "contact_page": lead.contact_page,
        "lead_tag": enr.lead_tag,
        "decision_maker_role": enr.decision_maker_role,
        "decision_maker_name": enr.decision_maker_name,
        "pain": enr.pain,
        "utp": enr.utp,
        "first_message": enr.first_message,
        "best_channel": enr.best_channel,
        "priority": enr.priority,
        "score": enr.score,
        "okved_hint": enr.okved_hint,
        "status": lead.status,
        "collected_at": lead.collected_at,
        "limitations": lead.limitations,
    }


# ──────────────────────────────────────────────────────────────────
# Generic webhook (для Shop-logistics / fulfillment / Make / Zapier)
# ──────────────────────────────────────────────────────────────────
class GenericWebhookCRM:
    def __init__(self, webhook_url: str | None = None, auth_token: str | None = None):
        self.webhook_url = (webhook_url or os.getenv("CRM_WEBHOOK_URL", "")).strip()
        self.auth_token = (auth_token or os.getenv("CRM_WEBHOOK_TOKEN", "")).strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    async def push(self, lead: LeadRecord) -> dict[str, Any]:
        if not self.is_configured:
            return {"ok": False, "error": "not_configured"}
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        payload = {
            "type": "lead",
            "source": "lead_ai_scraper",
            "lead": _lead_to_generic_payload(lead),
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(self.webhook_url, headers=headers, json=payload)
            return {"ok": resp.status_code < 400, "status": resp.status_code, "body": resp.text[:300]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}

    async def push_many(self, leads: list[LeadRecord]) -> dict[str, Any]:
        results = []
        for lead in leads:
            results.append(await self.push(lead))
        return {
            "ok": all(r.get("ok") for r in results),
            "total": len(results),
            "succeeded": sum(1 for r in results if r.get("ok")),
            "failed": [i for i, r in enumerate(results) if not r.get("ok")],
        }


# ──────────────────────────────────────────────────────────────────
# Bitrix24 incoming webhook
# ──────────────────────────────────────────────────────────────────
class Bitrix24CRM:
    """Использует incoming webhook вида:
    https://<portal>.bitrix24.ru/rest/<userId>/<token>/

    Создаёт сущность crm.lead.add.
    """

    def __init__(self, webhook_url: str | None = None):
        self.webhook_url = (webhook_url or os.getenv("BITRIX24_WEBHOOK_URL", "")).strip().rstrip("/")

    @property
    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    async def push(self, lead: LeadRecord) -> dict[str, Any]:
        if not self.is_configured:
            return {"ok": False, "error": "not_configured"}
        url = f"{self.webhook_url}/crm.lead.add.json"
        fields = {
            "TITLE": f"{lead.company} — {lead.site}",
            "COMPANY_TITLE": lead.legal_name or lead.company,
            "NAME": lead.enrichment.decision_maker_name or "",
            "SOURCE_ID": "WEB",
            "SOURCE_DESCRIPTION": lead.source_url or lead.site,
            "COMMENTS": (
                f"Боль: {lead.enrichment.pain}\nУТП: {lead.enrichment.utp}\n"
                f"Первое сообщение:\n{lead.enrichment.first_message}\n\n"
                f"ИНН: {lead.inn or ''} / ОГРН: {lead.ogrn or ''}"
            ),
            "OPPORTUNITY": lead.enrichment.score,
            "CURRENCY_ID": "RUB",
            "WEB": [{"VALUE": lead.site, "VALUE_TYPE": "WORK"}],
        }
        if lead.emails:
            fields["EMAIL"] = [{"VALUE": e, "VALUE_TYPE": "WORK"} for e in lead.emails]
        if lead.phones:
            fields["PHONE"] = [{"VALUE": p, "VALUE_TYPE": "WORK"} for p in lead.phones]
        payload = {"fields": fields, "params": {"REGISTER_SONET_EVENT": "Y"}}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, json=payload)
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            return {
                "ok": resp.status_code < 400 and "result" in data,
                "id": data.get("result"),
                "error": data.get("error_description") or resp.text[:200] if resp.status_code >= 400 else None,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}


# ──────────────────────────────────────────────────────────────────
# amoCRM (long-lived token)
# ──────────────────────────────────────────────────────────────────
class AmoCRM:
    def __init__(
        self,
        subdomain: str | None = None,
        access_token: str | None = None,
    ):
        self.subdomain = (subdomain or os.getenv("AMOCRM_SUBDOMAIN", "")).strip()
        self.access_token = (access_token or os.getenv("AMOCRM_ACCESS_TOKEN", "")).strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.subdomain and self.access_token)

    async def push(self, lead: LeadRecord) -> dict[str, Any]:
        if not self.is_configured:
            return {"ok": False, "error": "not_configured"}
        url = f"https://{self.subdomain}.amocrm.ru/api/v4/leads"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        body = [
            {
                "name": f"{lead.company} — {lead.site}",
                "price": lead.enrichment.score,
                "status_id": 0,
                "custom_fields_values": [],
                "_embedded": {
                    "contacts": [
                        {
                            "name": lead.enrichment.decision_maker_name or lead.company,
                            "custom_fields_values": [
                                *([{"field_code": "EMAIL", "values": [{"value": e, "enum_code": "WORK"} for e in lead.emails]}] if lead.emails else []),
                                *([{"field_code": "PHONE", "values": [{"value": p, "enum_code": "WORK"} for p in lead.phones]}] if lead.phones else []),
                            ],
                        }
                    ]
                },
            }
        ]
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, headers=headers, json=body)
            data = resp.json() if resp.text else {}
            ids = [item.get("id") for item in (data.get("_embedded", {}) or {}).get("leads", []) if item.get("id")]
            return {
                "ok": resp.status_code < 400,
                "ids": ids,
                "error": data.get("detail") if resp.status_code >= 400 else None,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}


# ──────────────────────────────────────────────────────────────────
# HubSpot (private app token)
# ──────────────────────────────────────────────────────────────────
class HubSpotCRM:
    def __init__(self, access_token: str | None = None):
        self.access_token = (access_token or os.getenv("HUBSPOT_ACCESS_TOKEN", "")).strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.access_token)

    async def push(self, lead: LeadRecord) -> dict[str, Any]:
        if not self.is_configured:
            return {"ok": False, "error": "not_configured"}
        url = "https://api.hubapi.com/crm/v3/objects/companies"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        properties = {
            "name": lead.legal_name or lead.company,
            "domain": lead.site,
            "city": lead.city,
            "industry": lead.enrichment.lead_tag,
            "phone": lead.phones[0] if lead.phones else "",
            "description": f"{lead.enrichment.pain}\n{lead.enrichment.utp}",
        }
        body = {"properties": {k: v for k, v in properties.items() if v}}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, headers=headers, json=body)
            data = resp.json() if resp.text else {}
            return {
                "ok": resp.status_code < 400,
                "id": data.get("id"),
                "error": data.get("message") if resp.status_code >= 400 else None,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}


# Реестр для удобного перебора в UI.
CRM_PROVIDERS = {
    "generic_webhook": GenericWebhookCRM,
    "bitrix24": Bitrix24CRM,
    "amocrm": AmoCRM,
    "hubspot": HubSpotCRM,
}


def get_configured_crms() -> dict[str, Any]:
    """Возвращает словарь {name: instance} только сконфигурированных CRM."""
    out = {}
    for name, cls in CRM_PROVIDERS.items():
        instance = cls()
        if instance.is_configured:
            out[name] = instance
    return out
