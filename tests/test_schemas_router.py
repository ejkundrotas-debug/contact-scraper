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


# ── v1.3: Logistics profile (Shop-logistics integration) ────────────────
def test_logistics_profile_default():
    """LogisticsProfile создаётся с пустыми дефолтами и валидным fit_score."""
    from modules.schemas import LogisticsProfile

    log = LogisticsProfile()
    assert log.fulfillment_fit_score == 0
    assert log.product_categories == []
    assert log.marketplaces == []
    assert log.monthly_orders_range == "не_определено"
    assert log.has_own_warehouse is None


def test_logistics_profile_from_llm_payload():
    """Парсим типичный JSON-ответ от LLM в фулфилмент-режиме."""
    from modules.schemas import LogisticsProfile

    payload = {
        "product_categories": ["косметика", "БАД"],
        "marketplaces": ["wildberries", "ozon"],
        "monthly_orders_range": "500_2000",
        "has_own_warehouse": False,
        "uses_fulfillment_now": True,
        "fulfillment_provider_current": "FBO Wildberries",
        "primary_regions": ["Москва", "регионы РФ"],
        "needs_marking": True,
        "fulfillment_fit_score": 9,
        "fit_reasoning": "идеальный e-commerce-клиент с маркетплейсами",
    }
    log = LogisticsProfile(**payload)
    assert log.fulfillment_fit_score == 9
    assert "косметика" in log.product_categories
    assert "wildberries" in log.marketplaces
    assert log.uses_fulfillment_now is True
    assert log.needs_marking is True


def test_logistics_fit_score_clamped():
    """fit_score должен быть в диапазоне 0-10 (Pydantic Field ge/le)."""
    from pydantic import ValidationError

    from modules.schemas import LogisticsProfile

    with pytest.raises(ValidationError):
        LogisticsProfile(fulfillment_fit_score=15)
    with pytest.raises(ValidationError):
        LogisticsProfile(fulfillment_fit_score=-1)


def test_lead_to_shop_logistics_payload():
    """Плоский payload для Shop-logistics CRM — без вложенностей."""
    from modules.crm import lead_to_shop_logistics_payload
    from modules.schemas import AIEnrichment, LeadRecord, LogisticsProfile

    lead = LeadRecord(
        company="Тестовый магазин",
        site="https://test-shop.ru",
        city="Москва",
        inn="7707083893",
        emails=["sales@test-shop.ru"],
        phones=["+79991234567"],
        enrichment=AIEnrichment(
            score=85,
            priority="high",
            first_message="Тестовое сообщение",
            best_channel="email",
            logistics=LogisticsProfile(
                product_categories=["косметика"],
                marketplaces=["wildberries", "собственный_сайт"],
                monthly_orders_range="500_2000",
                fulfillment_fit_score=8,
                needs_marking=True,
                primary_regions=["Москва", "регионы РФ"],
            ),
        ),
    )
    payload = lead_to_shop_logistics_payload(lead)
    # Плоская структура — не должно быть вложенных dict
    assert payload["company_name"] == "Тестовый магазин"
    assert payload["primary_email"] == "sales@test-shop.ru"
    assert payload["primary_phone"] == "+79991234567"
    assert payload["fulfillment_fit_score"] == 8
    assert "косметика" in payload["product_categories"]
    assert "wildberries" in payload["marketplaces"]
    assert payload["needs_marking_chestny_znak"] is True
    assert payload["general_score"] == 85
    # Не должно быть вложенных объектов
    for value in payload.values():
        assert not isinstance(value, dict), f"flat-payload required, got dict: {value}"


def test_shop_logistics_crm_not_configured():
    """ShopLogisticsCRM возвращает not_configured если нет webhook URL."""
    import os

    from modules.crm import ShopLogisticsCRM

    # Гарантируем отсутствие env
    os.environ.pop("SHOP_LOGISTICS_WEBHOOK_URL", None)
    crm = ShopLogisticsCRM()
    assert crm.is_configured is False


def test_storage_find_top_fulfillment_leads(tmp_path):
    """find_top_fulfillment_leads возвращает только лиды с fit_score >= min_fit."""
    from modules.schemas import AIEnrichment, LeadRecord, LogisticsProfile
    from modules.storage import LeadStorage

    storage = LeadStorage(db_path=str(tmp_path / "test_fulfillment.sqlite3"))

    # Лид с fit_score=9 (топ)
    high = LeadRecord(
        company="Хороший магазин",
        site="https://high.ru",
        enrichment=AIEnrichment(
            logistics=LogisticsProfile(fulfillment_fit_score=9, product_categories=["одежда"])
        ),
    )
    # Лид с fit_score=5 (средний)
    mid = LeadRecord(
        company="Средний магазин",
        site="https://mid.ru",
        enrichment=AIEnrichment(
            logistics=LogisticsProfile(fulfillment_fit_score=5)
        ),
    )
    # Лид без логистического профиля
    no_log = LeadRecord(company="Без профиля", site="https://no-log.ru")

    storage.upsert(high)
    storage.upsert(mid)
    storage.upsert(no_log)

    top = storage.find_top_fulfillment_leads(min_fit=7)
    assert len(top) == 1
    assert top[0].company == "Хороший магазин"
    assert top[0].enrichment.logistics.fulfillment_fit_score == 9

    # Снижаем порог — попадают оба с профилем
    top_lower = storage.find_top_fulfillment_leads(min_fit=5)
    assert len(top_lower) == 2
    assert top_lower[0].enrichment.logistics.fulfillment_fit_score == 9  # сортировка
    assert top_lower[1].enrichment.logistics.fulfillment_fit_score == 5
