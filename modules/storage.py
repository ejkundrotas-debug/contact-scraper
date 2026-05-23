from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from .schemas import LeadRecord

CSV_COLUMNS = [
    "Компания",
    "Сайт",
    "Город",
    "Ниша",
    "Контакт",
    "Телефон",
    "Email",
    "Telegram",
    "Соцсети",
    "Страница контактов",
    "ИНН",
    "ОГРН",
    "КПП",
    "ЛПР",
    "Тег",
    "Боль",
    "УТП",
    "Канал",
    "Статус",
    "Ответ",
    "Первое сообщение",
    "Приоритет",
    "Скоринг",
    "Лог.фит (0-10)",
    "Категории товаров",
    "Маркетплейсы",
    "Объём заказов/мес",
    "Регионы",
    "Источник",
    "Дата сбора",
    "Ограничения",
]


class LeadStorage:
    """SQLite storage with WAL mode and concurrent-safe upsert.

    Использует один pragma'нутый соединение на инициализации.
    Каждый upsert открывает короткое соединение с WAL + busy_timeout, что
    позволяет 10+ конкурентным таскам параллельно писать без 'database is locked'.
    """

    def __init__(self, db_path: str | Path = "data/leads.sqlite3"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self._init_lock, self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company TEXT NOT NULL,
                    site TEXT NOT NULL UNIQUE,
                    city TEXT,
                    niche TEXT,
                    note TEXT,
                    contact TEXT,
                    phones TEXT,
                    emails TEXT,
                    telegram TEXT,
                    social_links TEXT,
                    contact_page TEXT,
                    source_url TEXT,
                    inn TEXT,
                    ogrn TEXT,
                    kpp TEXT,
                    legal_name TEXT,
                    legal_address TEXT,
                    collected_at TEXT,
                    status TEXT,
                    response TEXT,
                    enrichment TEXT,
                    limitations TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Если таблица существовала до миграции — добавим недостающие колонки.
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
            for col in ("inn", "ogrn", "kpp", "legal_name", "legal_address"):
                if col not in existing_cols:
                    try:
                        conn.execute(f"ALTER TABLE leads ADD COLUMN {col} TEXT")
                    except sqlite3.OperationalError:
                        pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_inn ON leads(inn)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_ogrn ON leads(ogrn)")
            # BUG-1 fix: индекс на json_extract создаём только если функция доступна.
            try:
                conn.execute("SELECT json_extract('{}', '$.x')")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_leads_tag ON leads(json_extract(enrichment, '$.lead_tag'))"
                )
            except sqlite3.OperationalError:
                # SQLite без JSON1, пропускаем.
                pass

    def upsert(self, lead: LeadRecord) -> None:
        payload = lead.model_dump(mode="json")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO leads (
                    company, site, city, niche, note, contact, phones, emails, telegram,
                    social_links, contact_page, source_url, inn, ogrn, kpp, legal_name, legal_address,
                    collected_at, status, response, enrichment, limitations
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(site) DO UPDATE SET
                    company=excluded.company,
                    city=excluded.city,
                    niche=excluded.niche,
                    note=excluded.note,
                    contact=excluded.contact,
                    phones=excluded.phones,
                    emails=excluded.emails,
                    telegram=excluded.telegram,
                    social_links=excluded.social_links,
                    contact_page=excluded.contact_page,
                    source_url=excluded.source_url,
                    inn=COALESCE(excluded.inn, leads.inn),
                    ogrn=COALESCE(excluded.ogrn, leads.ogrn),
                    kpp=COALESCE(excluded.kpp, leads.kpp),
                    legal_name=COALESCE(excluded.legal_name, leads.legal_name),
                    legal_address=COALESCE(excluded.legal_address, leads.legal_address),
                    collected_at=excluded.collected_at,
                    status=excluded.status,
                    response=excluded.response,
                    enrichment=excluded.enrichment,
                    limitations=excluded.limitations,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    payload["company"],
                    payload["site"],
                    payload.get("city", ""),
                    payload.get("niche", ""),
                    payload.get("note", ""),
                    payload.get("contact", ""),
                    json.dumps(payload.get("phones", []), ensure_ascii=False),
                    json.dumps(payload.get("emails", []), ensure_ascii=False),
                    json.dumps(payload.get("telegram", []), ensure_ascii=False),
                    json.dumps(payload.get("social_links", []), ensure_ascii=False),
                    payload.get("contact_page"),
                    payload.get("source_url"),
                    payload.get("inn"),
                    payload.get("ogrn"),
                    payload.get("kpp"),
                    payload.get("legal_name"),
                    payload.get("legal_address"),
                    payload.get("collected_at"),
                    payload.get("status"),
                    payload.get("response", ""),
                    json.dumps(payload.get("enrichment", {}), ensure_ascii=False),
                    json.dumps(payload.get("limitations", []), ensure_ascii=False),
                ),
            )

    def upsert_many(self, leads: Iterable[LeadRecord]) -> int:
        count = 0
        for lead in leads:
            self.upsert(lead)
            count += 1
        return count

    def list_leads(self, limit: int = 500) -> list[LeadRecord]:
        rows = []
        with self.connect() as conn:
            cur = conn.execute("SELECT * FROM leads ORDER BY updated_at DESC LIMIT ?", (limit,))
            rows = cur.fetchall()
        return [self._row_to_lead(row) for row in rows]

    def find_by_inn(self, inn: str) -> LeadRecord | None:
        if not inn:
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM leads WHERE inn = ? LIMIT 1", (inn,)).fetchone()
        return self._row_to_lead(row) if row else None

    def find_by_ogrn(self, ogrn: str) -> LeadRecord | None:
        if not ogrn:
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM leads WHERE ogrn = ? LIMIT 1", (ogrn,)).fetchone()
        return self._row_to_lead(row) if row else None

    def find_by_site(self, site: str) -> LeadRecord | None:
        if not site:
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM leads WHERE site = ? LIMIT 1", (site,)).fetchone()
        return self._row_to_lead(row) if row else None

    def update_status(self, site: str, status: str, response: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE leads SET status=?, response=?, updated_at=CURRENT_TIMESTAMP WHERE site=?",
                (status, response, site),
            )

    def delete(self, site: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM leads WHERE site=?", (site,))

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM leads").fetchone()["c"]
            by_status = {
                row["status"]: row["c"]
                for row in conn.execute("SELECT status, COUNT(*) AS c FROM leads GROUP BY status").fetchall()
            }
            with_email = conn.execute("SELECT COUNT(*) AS c FROM leads WHERE emails NOT IN ('[]', '', NULL)").fetchone()["c"]
            with_inn = conn.execute("SELECT COUNT(*) AS c FROM leads WHERE inn IS NOT NULL AND inn != ''").fetchone()["c"]
        return {"total": total, "by_status": by_status, "with_email": with_email, "with_inn": with_inn}

    def _row_to_lead(self, row: sqlite3.Row) -> LeadRecord:
        def loads(value: str | None, default):
            if not value:
                return default
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return default

        def col(name: str, default=None):
            try:
                return row[name]
            except (IndexError, KeyError):
                return default

        return LeadRecord(
            company=row["company"],
            site=row["site"],
            city=row["city"] or "",
            niche=row["niche"] or "",
            note=row["note"] or "",
            contact=row["contact"] or "",
            phones=loads(row["phones"], []),
            emails=loads(row["emails"], []),
            telegram=loads(row["telegram"], []),
            social_links=loads(row["social_links"], []),
            contact_page=row["contact_page"],
            source_url=row["source_url"],
            inn=col("inn"),
            ogrn=col("ogrn"),
            kpp=col("kpp"),
            legal_name=col("legal_name"),
            legal_address=col("legal_address"),
            collected_at=row["collected_at"],
            status=row["status"] or "new",
            response=row["response"] or "",
            enrichment=loads(row["enrichment"], {}),
            limitations=loads(row["limitations"], []),
        )

    def to_csv_rows(self, leads: Iterable[LeadRecord]) -> list[dict[str, str | int]]:
        rows = []
        for lead in leads:
            rows.append(
                {
                    "Компания": lead.company,
                    "Сайт": lead.site,
                    "Город": lead.city,
                    "Ниша": lead.niche,
                    "Контакт": lead.contact,
                    "Телефон": "; ".join(lead.phones),
                    "Email": "; ".join(lead.emails),
                    "Telegram": "; ".join(lead.telegram),
                    "Соцсети": "; ".join(lead.social_links),
                    "Страница контактов": lead.contact_page or "",
                    "ИНН": lead.inn or "",
                    "ОГРН": lead.ogrn or "",
                    "КПП": lead.kpp or "",
                    "ЛПР": lead.enrichment.decision_maker_name or lead.enrichment.decision_maker_role,
                    "Тег": lead.enrichment.lead_tag,
                    "Боль": lead.enrichment.pain,
                    "УТП": lead.enrichment.utp,
                    "Канал": lead.enrichment.best_channel,
                    "Статус": lead.status,
                    "Ответ": lead.response,
                    "Первое сообщение": lead.enrichment.first_message,
                    "Приоритет": lead.enrichment.priority,
                    "Скоринг": lead.enrichment.score,
                    "Лог.фит (0-10)": (lead.enrichment.logistics.fulfillment_fit_score if lead.enrichment.logistics else ""),
                    "Категории товаров": ("; ".join(lead.enrichment.logistics.product_categories) if lead.enrichment.logistics else ""),
                    "Маркетплейсы": ("; ".join(lead.enrichment.logistics.marketplaces) if lead.enrichment.logistics else ""),
                    "Объём заказов/мес": (lead.enrichment.logistics.monthly_orders_range if lead.enrichment.logistics else ""),
                    "Регионы": ("; ".join(lead.enrichment.logistics.primary_regions) if lead.enrichment.logistics else ""),
                    "Источник": lead.source_url or lead.site,
                    "Дата сбора": lead.collected_at,
                    "Ограничения": "; ".join(lead.limitations),
                }
            )
        return rows

    def find_top_fulfillment_leads(self, min_fit: int = 7, limit: int = 100) -> list[LeadRecord]:
        """Возвращает лиды с высоким fulfillment_fit_score (для приоритезации
        логистических сейлзов).
        Работает на любом SQLite (с JSON1 и без) — фильтрация делается в Python.
        """
        leads = self.list_leads(limit=1000)
        out = []
        for lead in leads:
            if lead.enrichment.logistics and lead.enrichment.logistics.fulfillment_fit_score >= min_fit:
                out.append(lead)
            if len(out) >= limit:
                break
        out.sort(key=lambda x: -(x.enrichment.logistics.fulfillment_fit_score if x.enrichment.logistics else 0))
        return out
