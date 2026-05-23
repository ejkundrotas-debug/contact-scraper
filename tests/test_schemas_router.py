import asyncio

import pytest

from modules.router import MultiProviderRouter, estimate_tokens
from modules.schemas import (
    ContactExtraction,
    LeadRecord,
    is_platform_email,
    _normalize_phone,
)


# ── Phone/email validation (BUG-3 regression) ───────────────────────────
def test_contact_soft_validation():
    ce = ContactExtraction(
        phones=["000-00-00", "9999999", "8 (977) 484-74-68", "+7 (977) 484-74-68"],
        emails=["bad", "Info@Example.RU"],
    )
    # +79774847468 нормализуется из обоих форматов
    assert "+79774847468" in ce.phones
    # 7-значное "9999999" теперь отбрасывается (повышен порог)
    assert "9999999" not in ce.phones
    assert "0000000" not in ce.phones
    # example.ru теперь в blacklist
    assert "info@example.ru" not in ce.emails


def test_phone_blacklist_patterns():
    assert _normalize_phone("12345678901") is None  # явно тестовый
    assert _normalize_phone("1111111111") is None
    assert _normalize_phone("0000000000") is None
    assert _normalize_phone("+44 20 7946 0958") == "+442079460958"  # UK
    assert _normalize_phone("89169876543") == "+79169876543"  # 8-prefix → +7


def test_platform_email_detection():
    assert is_platform_email("noreply@company.com") is True
    assert is_platform_email("no-reply@company.com") is True
    assert is_platform_email("info@company.com") is False
    assert is_platform_email("") is False
    assert is_platform_email("not-an-email") is False


# ── JSON parsing (router) ───────────────────────────────────────────────
def test_json_markdown_parsing():
    router = MultiProviderRouter("configs/config_default.yaml")
    assert router._validate_with_schema('```json\n{"a": 1}\n```') == {"a": 1}
    assert router._validate_with_schema('before {"b": [1, 2]} after') == {"b": [1, 2]}
    # Уже dict
    assert router._validate_with_schema({"x": 1}) == {"x": 1}
    # JSON в конце без обрамления
    assert router._validate_with_schema('text {"c": 3} trailing }') == {"c": 3}


# ── Token estimation (BUG-6) ────────────────────────────────────────────
def test_token_estimation_cyrillic():
    # Кириллица: 1 токен на 2 символа
    rus = "извлеки контакты с сайта"  # 24 символа
    eng = "extract contacts from site"  # 26 символов
    rus_est = estimate_tokens(rus)
    eng_est = estimate_tokens(eng)
    # Русский должен оцениваться выше или равно (больше токенов на символ)
    assert rus_est >= eng_est - 2
    assert rus_est > 0
    assert estimate_tokens("") == 0


# ── Router pick logic ───────────────────────────────────────────────────
def test_router_pick_no_provider_without_keys(monkeypatch):
    # Убираем все возможные ключи
    for env in [
        "GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY", "TOGETHER_API_KEY", "XAI_API_KEY",
        "GIGACHAT_AUTH_KEY", "GIGACHAT_ACCESS_TOKEN", "YANDEXGPT_API_KEY",
    ]:
        monkeypatch.delenv(env, raising=False)
    router = MultiProviderRouter("configs/config_default.yaml")
    chosen = asyncio.run(router.pick("extract"))
    assert chosen is None


# ── Storage WAL & dedupe by INN ────────────────────────────────────────
def test_storage_wal_and_inn_dedupe(tmp_path):
    from modules.storage import LeadStorage

    db = tmp_path / "test.sqlite3"
    storage = LeadStorage(db_path=str(db))
    lead1 = LeadRecord(company="Ромашка", site="https://romashka.ru", inn="7707083893")
    lead2 = LeadRecord(company="Ромашка-копия", site="https://romashka2.ru", inn="7707083893")
    storage.upsert(lead1)
    storage.upsert(lead2)

    # Поиск по ИНН возвращает первый найденный (любой из двух)
    found = storage.find_by_inn("7707083893")
    assert found is not None
    assert found.inn == "7707083893"

    # find_by_site — точный матч
    f1 = storage.find_by_site("https://romashka.ru")
    assert f1 is not None and f1.company == "Ромашка"


def test_storage_csv_export(tmp_path):
    from modules.storage import LeadStorage

    db = tmp_path / "csv.sqlite3"
    storage = LeadStorage(db_path=str(db))
    lead = LeadRecord(
        company="Test Ltd",
        site="https://test.ru",
        inn="7707083893",
        ogrn="1027700132195",
        emails=["info@test.ru"],
    )
    storage.upsert(lead)
    rows = storage.to_csv_rows(storage.list_leads())
    assert len(rows) == 1
    row = rows[0]
    assert row["ИНН"] == "7707083893"
    assert row["ОГРН"] == "1027700132195"
    assert row["Email"] == "info@test.ru"


# ── Scraper SSRF guard (SEC-1) ──────────────────────────────────────────
def test_scraper_blocks_ssrf():
    from modules.scraper import PublicScraper

    scraper = PublicScraper(respect_robots=False, allow_private_ips=False)
    assert scraper._is_ssrf_target("http://127.0.0.1:6379/") is True
    assert scraper._is_ssrf_target("http://localhost:8080/") is True
    assert scraper._is_ssrf_target("http://169.254.169.254/latest/") is True
    assert scraper._is_ssrf_target("http://10.0.0.1/") is True
    assert scraper._is_ssrf_target("http://192.168.1.1/") is True
    # Публичный домен
    assert scraper._is_ssrf_target("https://example.com/") is False


# ── INN/OGRN extraction ─────────────────────────────────────────────────
def test_inn_ogrn_extraction():
    from modules.scraper import PublicScraper

    scraper = PublicScraper(respect_robots=False)
    html = """
    <html><body>
    Контакты: info@example-real.ru, +7 (495) 123-45-67
    Реквизиты: ООО "Ромашка", ИНН 7707083893, ОГРН 1027700132195, КПП 770701001
    </body></html>
    """
    ce = scraper.extract_contacts_from_html("https://example-real.ru", html)
    assert ce.inn == "7707083893"
    assert ce.ogrn == "1027700132195"
    assert ce.kpp == "770701001"


# ── Pipeline parse_manual_rows ──────────────────────────────────────────
def test_parse_manual_rows_skips_header():
    from modules.pipeline import parse_manual_rows

    text = "Компания;Сайт;Город;Ниша;Заметка\nРомашка;https://r.ru;Москва;ecom;test"
    rows = parse_manual_rows(text)
    assert len(rows) == 1
    assert rows[0].company == "Ромашка"
    assert rows[0].site == "https://r.ru"


# ── DaData client (stub-friendly) ───────────────────────────────────────
def test_dadata_not_configured_without_key(monkeypatch):
    from modules.dadata import DaDataClient

    monkeypatch.delenv("DADATA_API_KEY", raising=False)
    client = DaDataClient()
    assert client.is_configured is False
    assert asyncio.run(client.find_by_inn("7707083893")) is None


# ── CRM not configured ──────────────────────────────────────────────────
def test_crm_not_configured(monkeypatch):
    from modules.crm import GenericWebhookCRM

    monkeypatch.delenv("CRM_WEBHOOK_URL", raising=False)
    crm = GenericWebhookCRM()
    assert crm.is_configured is False
    lead = LeadRecord(company="X", site="https://x.ru")
    result = asyncio.run(crm.push(lead))
    assert result == {"ok": False, "error": "not_configured"}


# ── Combined prompt path doesn't crash on AI error ──────────────────────
@pytest.mark.asyncio
async def test_pipeline_combined_fallback(monkeypatch, tmp_path):
    from modules.pipeline import LeadPipeline, ParsedInputRow
    from modules.storage import LeadStorage

    # Заглушка-pipeline без сетевых вызовов:
    pipeline = LeadPipeline(storage=LeadStorage(db_path=str(tmp_path / "p.sqlite3")))

    async def fake_call_json(*args, **kwargs):
        return {"error": "all_providers_failed", "details": "no provider"}

    async def fake_scrape(site):
        from modules.schemas import ContactExtraction

        return ContactExtraction(site=site, source_url=site), ""

    monkeypatch.setattr(pipeline.router, "call_json", fake_call_json)
    monkeypatch.setattr(pipeline.scraper, "scrape_company_site", fake_scrape)

    row = ParsedInputRow(company="Test", site="https://example-test.ru")
    lead = await pipeline.process_candidate(row)
    # Без LLM — статус needs_review, но fallback_enrichment всё равно даёт first_message
    assert lead.status in {"needs_review", "draft_ready"}
    assert "fallback_enrichment" in lead.enrichment.risks
