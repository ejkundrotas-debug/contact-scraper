from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

LeadTag = Literal[
    "интернет-магазин",
    "производитель",
    "услуги",
    "логистика",
    "B2B",
    "не подходит",
    "не определено",
]

Priority = Literal["high", "medium", "low", "unknown"]
Channel = Literal["email", "telegram", "phone", "manual", "unknown"]
Status = Literal[
    "new",
    "parsed",
    "enriched",
    "needs_review",
    "draft_ready",
    "sent_manually",
    "answered",
    "rejected",
]

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")
# Более строгая регулярка телефонов: либо +country, либо 8/7 префикс, либо 10-12 цифр подряд
# с разделителями. Минимум 10 цифр для отсечения артикулов.
PHONE_RE = re.compile(
    r"(?:(?:\+?\d{1,3}[\s\-]?)?\(?\d{2,5}\)?[\s\-]?\d{2,4}[\s\-]?\d{2,4}(?:[\s\-]?\d{2,4})?)"
)
TG_RE = re.compile(r"(?i)(?:https?://)?t\.me/[a-zA-Z0-9_]{4,32}|@[a-zA-Z0-9_]{4,32}")

# ИНН/ОГРН/КПП для РФ (10/12 цифр и 13/15 цифр соответственно)
INN_RE = re.compile(r"(?<!\d)(\d{10}|\d{12})(?!\d)")
OGRN_RE = re.compile(r"(?<!\d)(\d{13}|\d{15})(?!\d)")
KPP_RE = re.compile(r"(?<!\d)(\d{9})(?!\d)")

DEMO_PHONE_PATTERNS = (
    re.compile(r"^0{7,}$"),
    re.compile(r"^9{7,}$"),
    re.compile(r"^(\d)\1{6,}$"),
    re.compile(r"^1234567\d*$"),  # явные тестовые
    re.compile(r"^(?:12345|54321|11111|00000)\d*$"),
)

# Блэклист префиксов (регистрационные номера, бывшие тестовые)
BLACKLIST_DOMAINS = {
    "example.com", "example.ru", "example.org",
    "test.com", "test.ru",
    "localhost",
}

# E-mail от платформ, а не от компании
_PLATFORM_EMAIL_HINTS = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "webmaster", "postmaster", "abuse", "hostmaster",
)


def _normalize_phone(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    digits = re.sub(r"\D+", "", raw)
    # Soft-validation: не роняем весь объект, просто отбрасываем мусор.
    # Повышаем нижний порог до 10 цифр — отсекает артикулы/номера документов.
    if len(digits) < 10 or len(digits) > 15:
        return None
    if any(p.match(digits) for p in DEMO_PHONE_PATTERNS):
        return None
    if set(digits) <= {"0"}:
        return None
    if raw.startswith("+"):
        return "+" + digits
    # РФ/СНГ-friendly нормализация, без агрессивного отбрасывания городских номеров.
    if len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    if len(digits) == 10:
        # Мобильный РФ без кода страны: 9XXXXXXXXX
        if digits.startswith("9"):
            return "+7" + digits
    return "+" + digits if not digits.startswith("+") else digits


def _normalize_email(value: Any) -> str | None:
    if value is None:
        return None
    email = str(value).strip().lower()
    # Обрезаем querystring из mailto:
    email = email.split("?")[0].split("#")[0]
    if not EMAIL_RE.fullmatch(email):
        return None
    domain = email.split("@", 1)[-1]
    if domain in BLACKLIST_DOMAINS:
        return None
    # Частые технические/примерные адреса можно оставить, но пометить в downstream.
    return email


def is_platform_email(email: str) -> bool:
    """Mailbox-name похож на технический, а не на контакт компании."""
    if not email or "@" not in email:
        return False
    local = email.split("@", 1)[0].lower()
    return any(hint in local for hint in _PLATFORM_EMAIL_HINTS)


def _normalize_url(value: Any) -> str | None:
    if value is None:
        return None
    url = str(value).strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    return url


class ContactExtraction(BaseModel):
    company: str = ""
    site: str = ""
    contact_page: str | None = None
    phones: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    telegram: list[str] = Field(default_factory=list)
    social_links: list[str] = Field(default_factory=list)
    inn: str | None = None
    ogrn: str | None = None
    kpp: str | None = None
    source_url: str | None = None
    collected_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    limitations: list[str] = Field(default_factory=list)

    @field_validator("site", "contact_page", "source_url", mode="before")
    @classmethod
    def normalize_urls(cls, value: Any) -> str | None:
        return _normalize_url(value) if value else value

    @field_validator("phones", mode="before")
    @classmethod
    def normalize_phones(cls, value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        out: list[str] = []
        for item in values:
            phone = _normalize_phone(item)
            if phone and phone not in out:
                out.append(phone)
        return out

    @field_validator("emails", mode="before")
    @classmethod
    def normalize_emails(cls, value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        out: list[str] = []
        for item in values:
            email = _normalize_email(item)
            if email and email not in out:
                out.append(email)
        return out

    @field_validator("telegram", "social_links", "limitations", mode="before")
    @classmethod
    def normalize_str_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        out: list[str] = []
        for item in values:
            s = str(item).strip()
            if s and s not in out:
                out.append(s)
        return out


class AIEnrichment(BaseModel):
    lead_tag: LeadTag = "не определено"
    decision_maker_role: str = ""
    decision_maker_name: str | None = None
    pain: str = ""
    utp: str = ""
    first_message: str = ""
    best_channel: Channel = "unknown"
    priority: Priority = "unknown"
    okved_hint: str | None = None
    score: int = Field(default=0, ge=0, le=100)
    reasoning_short: str = ""
    is_relevant: bool = True
    risks: list[str] = Field(default_factory=list)

    @field_validator("risks", mode="before")
    @classmethod
    def normalize_risks(cls, value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        return [str(v).strip() for v in values if str(v).strip()]


class LeadRecord(BaseModel):
    company: str
    site: str
    city: str = ""
    niche: str = ""
    note: str = ""
    contact: str = ""
    phones: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    telegram: list[str] = Field(default_factory=list)
    social_links: list[str] = Field(default_factory=list)
    contact_page: str | None = None
    source_url: str | None = None
    # Официальные реквизиты (РФ) — для дедупликации и КРМ
    inn: str | None = None
    ogrn: str | None = None
    kpp: str | None = None
    legal_name: str | None = None
    legal_address: str | None = None
    collected_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: Status = "new"
    response: str = ""
    enrichment: AIEnrichment = Field(default_factory=AIEnrichment)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def set_contact(self) -> "LeadRecord":
        if not self.contact:
            self.contact = self.emails[0] if self.emails else (self.telegram[0] if self.telegram else (self.phones[0] if self.phones else ""))
        return self

    @property
    def lead_tag(self) -> str:
        return self.enrichment.lead_tag

    @property
    def utp(self) -> str:
        return self.enrichment.utp

    @property
    def first_message(self) -> str:
        return self.enrichment.first_message


class SearchCandidate(BaseModel):
    title: str = ""
    url: str
    snippet: str = ""
    source: str = ""
    city: str = ""
    niche: str = ""

    @field_validator("url", mode="before")
    @classmethod
    def normalize_candidate_url(cls, value: Any) -> str:
        url = _normalize_url(value)
        if not url:
            raise ValueError("Invalid URL")
        return url


class PipelineStats(BaseModel):
    discovered: int = 0
    parsed: int = 0
    enriched: int = 0
    saved: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
