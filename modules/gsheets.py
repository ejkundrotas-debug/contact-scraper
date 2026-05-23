"""Google Sheets export.

Два варианта:
1. Service Account (рекомендовано) — GOOGLE_SERVICE_ACCOUNT_JSON в .env, путь к JSON-файлу.
2. Apps Script Webhook — GSHEETS_WEBHOOK_URL, простой POST с JSON-массивом строк.

Для service-account-варианта используется gspread + google-auth (lazy import,
зависимости опциональны — если не установлены, выдаём понятную ошибку в UI).
"""
from __future__ import annotations

import json
import os
from typing import Iterable

import httpx

from .storage import CSV_COLUMNS


class GoogleSheetsExporter:
    def __init__(
        self,
        service_account_json: str | None = None,
        webhook_url: str | None = None,
    ):
        self.service_account_json = (service_account_json or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")).strip()
        self.webhook_url = (webhook_url or os.getenv("GSHEETS_WEBHOOK_URL", "")).strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.service_account_json or self.webhook_url)

    @property
    def mode(self) -> str:
        if self.service_account_json:
            return "service_account"
        if self.webhook_url:
            return "webhook"
        return "none"

    async def export_via_webhook(self, rows: list[dict]) -> dict:
        if not self.webhook_url:
            return {"ok": False, "error": "no_webhook_url"}
        headers = {"Content-Type": "application/json"}
        token = os.getenv("GSHEETS_WEBHOOK_TOKEN", "").strip()
        if token:
            headers["X-Auth-Token"] = token
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    self.webhook_url,
                    headers=headers,
                    json={"columns": CSV_COLUMNS, "rows": rows},
                )
            return {"ok": resp.status_code < 400, "status": resp.status_code, "body": resp.text[:500]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}

    def export_via_service_account(
        self,
        rows: list[dict],
        spreadsheet_id: str,
        worksheet_name: str = "Leads",
    ) -> dict:
        """Synchronous Google Sheets export via service account.
        Requires: pip install gspread google-auth
        """
        try:
            import gspread  # type: ignore
            from google.oauth2.service_account import Credentials  # type: ignore
        except ImportError:
            return {
                "ok": False,
                "error": "install_deps",
                "details": "pip install gspread google-auth",
            }

        try:
            # service_account_json — может быть путь к файлу или сам JSON.
            if os.path.isfile(self.service_account_json):
                creds = Credentials.from_service_account_file(
                    self.service_account_json,
                    scopes=["https://www.googleapis.com/auth/spreadsheets"],
                )
            else:
                info = json.loads(self.service_account_json)
                creds = Credentials.from_service_account_info(
                    info,
                    scopes=["https://www.googleapis.com/auth/spreadsheets"],
                )
            client = gspread.authorize(creds)
            sh = client.open_by_key(spreadsheet_id)
            try:
                ws = sh.worksheet(worksheet_name)
            except Exception:
                ws = sh.add_worksheet(title=worksheet_name, rows=max(1000, len(rows) + 10), cols=len(CSV_COLUMNS))

            # Перезаписываем шапку и данные.
            ws.clear()
            data = [CSV_COLUMNS] + [
                [str(row.get(col, "")) for col in CSV_COLUMNS] for row in rows
            ]
            ws.update(values=data, range_name="A1")
            return {"ok": True, "rows": len(rows), "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}

    async def export(
        self,
        rows: Iterable[dict],
        spreadsheet_id: str | None = None,
        worksheet_name: str = "Leads",
    ) -> dict:
        rows_list = list(rows)
        if self.service_account_json and spreadsheet_id:
            return self.export_via_service_account(rows_list, spreadsheet_id, worksheet_name)
        if self.webhook_url:
            return await self.export_via_webhook(rows_list)
        return {"ok": False, "error": "exporter_not_configured"}
