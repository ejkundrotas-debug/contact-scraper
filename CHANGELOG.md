# Changelog

Все значимые изменения проекта документируются в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
проект следует [Semantic Versioning](https://semver.org/lang/ru/).

## [1.3.0] — 2026-05-23

### Добавлено — Shop-logistics / Фулфилмент интеграция
- **`LogisticsProfile`** (`modules/schemas.py`) — логистический подпрофиль лида:
  - Категории товаров (одежда, косметика, БАД, электроника, БАД, хрупкое, опасные грузы...)
  - Маркетплейсы (Wildberries, Ozon, Yandex.Market, KazanExpress, Lamoda...)
  - Объём заказов в месяц (до_100 / 100_500 / 500_2000 / 2000_10000 / 10000_plus)
  - Логистические флаги: свой склад, текущий фулфилмент, нужен ли холод/маркировка ЧЗ/обработка возвратов
  - География (Москва / СПб / регионы РФ / СНГ / ЕС)
  - **fulfillment_fit_score 0-10** — отдельный скоринг под фулфилмент-бизнес
  - logistics_pain — болевая точка из текущей логистики
- **`FULFILLMENT_PROMPT`** (`modules/prompts.py`) — специализированный prompt, заменяет combined в фулфилмент-режиме
- **Фулфилмент-режим** (`modules/pipeline.py::_enrich_fulfillment`) — toggle в sidebar активирует logistics-extraction
- **`ShopLogisticsCRM`** (`modules/crm.py`) — пятый CRM-провайдер, плоский payload специально под фулфилмент-CRM
- **`lead_to_shop_logistics_payload()`** — конвертер LeadRecord → плоский JSON с логистическими полями верхнего уровня
- **`storage.find_top_fulfillment_leads(min_fit=7)`** — приоритезация по fit_score
- **UI обновления**:
  - Sidebar toggle 🚚 «Фулфилмент-режим»
  - В табе «Лиды» — expander «Топ-N логистических лидов» с CSV-экспортом
  - В табе «Интеграции» — radio-кнопка «🚚 Топ-фулфилмент (fit_score≥7)» для CRM-пуша
- **CSV-экспорт** теперь включает 5 новых колонок: «Лог.фит», «Категории товаров», «Маркетплейсы», «Объём заказов/мес», «Регионы»
- **Env vars**: `SHOP_LOGISTICS_WEBHOOK_URL`, `SHOP_LOGISTICS_TOKEN`
- **11 новых unit-тестов** для LogisticsProfile, ShopLogisticsCRM, payload, find_top — всего **25/25 проходят**

### Изменено
- Версия проекта: **1.2.0 → 1.3.0**
- `CRM_PROVIDERS` теперь 5 провайдеров (добавлен `shop_logistics`)
- `requirements.txt` без изменений — функционал работает на текущем стеке

## [1.2.0] — 2026-05-23

### Добавлено
- **18 LLM-провайдеров** в 5 API-форматах:
  - OpenAI-compatible: Groq, OpenRouter, DeepSeek, Mistral, Together AI, xAI Grok, OpenAI GPT-4o/4o-mini
  - Anthropic Messages API: Claude 3.5 Haiku, Claude 3.5 Sonnet
  - Google Gemini native v1beta
  - Yandex Foundation Models (YandexGPT)
  - Sber GigaChat с автообновлением OAuth-токена
- **DaData API** (`modules/dadata.py`) — обогащение по ИНН/ОГРН: юр. название, адрес, ОКВЭД, руководитель
- **Дедупликация лидов** по ИНН → ОГРН → site (`modules/storage.py::find_by_inn/find_by_ogrn`)
- **Combined-prompt режим** (`modules/pipeline.py::_enrich_combined`) — 1 LLM-вызов вместо 3 (validate + extract + score), экономия ~60% токенов
- **Google Sheets экспорт** (`modules/gsheets.py`) — service account (gspread) или webhook
- **4 CRM-клиента** (`modules/crm.py`):
  - `GenericWebhookCRM` — универсальный webhook для Shop-logistics / n8n / Make / Zapier
  - `Bitrix24CRM` — incoming webhook
  - `AmoCRM` v4 (Bearer token)
  - `HubSpotCRM` v3 (Private App token)
- **Извлечение ИНН/ОГРН/КПП** из HTML-контента (`modules/scraper.py`)
- **3 новых таба** в UI: 🔌 Интеграции, 📊 Дашборд, 🤖 Провайдеры (редизайн)
- **Docker** — `Dockerfile` (python:3.11-slim + lxml + healthcheck) и `docker-compose.yml` (volume + env_file + опциональный Caddy)
- **Unit-тесты** — 14 тестов покрывают: phone/email валидацию, JSON-парсинг, token estimation, router pick logic, SQLite WAL + dedupe, SSRF guard, ИНН/ОГРН regex, pipeline fallback
- **Аудит-отчёт** `AUDIT.md` — 30+ задокументированных проблем (9 BUG / 6 SEC / 6 PERF / 9 LOGIC) с приоритетами
- **Cloudflare-архитектура** `CLOUDFLARE_ARCHITECTURE.md` — гибридный план миграции (Hono Pages + D1 + Queues + Python worker на VPS)

### Изменено
- **`modules/router.py`** — переписан под 5 API-форматов, добавлено `estimate_tokens()` с учётом кириллицы (1 токен ≈ 2 символа vs 3 для латиницы)
- **`modules/scraper.py`** — SSRF-защита через `ipaddress` (фильтр приватных/loopback/link-local IP), лимит 10 MB на ответ, per-domain `asyncio.Lock`
- **`modules/storage.py`** — SQLite WAL mode + `busy_timeout=30000` + автомиграция новых колонок (inn, ogrn, kpp, legal_name, legal_address)
- **`modules/prompts.py`** — защита от prompt injection через делимитеры `<<<USER_CONTENT_START>>>...<<<USER_CONTENT_END>>>`
- **`modules/schemas.py`** — ужесточён `PHONE_RE` (минимум 10 цифр, чтобы не подхватывать артикулы), добавлены `INN_RE`/`OGRN_RE`/`KPP_RE`, `BLACKLIST_DOMAINS`, `is_platform_email()`
- **`requirements.txt`** — убрана неиспользуемая Playwright (~400 MB), добавлены `lxml`, `pytest`, `pytest-asyncio`
- **`configs/config_default.yaml`** и **`configs/config_ru_first.yaml`** — 18 провайдеров с явным `api_format` и `extras` для адаптеров

### Исправлено
- **BUG-2**: pipeline корректно проверяет `is_company_contact` из ответа LLM
- **BUG-4**: cache poisoning — не кэшируем отказные ответы `is_company_contact: false`
- **BUG-5**: явная сортировка по `(-priority, -remaining_ratio)` в router
- **BUG-6**: правильная оценка токенов для кириллицы
- **BUG-7**: `asyncio.gather(*tasks, return_exceptions=True)` — одна упавшая задача больше не валит весь батч
- **BUG-8**: email/телефон извлекаются раздельно из текста и `href`-атрибутов (раньше пересекались)
- **BUG-9**: content-type check переписан явно с whitelist
- **JSON parser edge case**: переписан `_validate_with_schema` через `json.JSONDecoder.raw_decode` + balanced-bracket fallback (корректно обрабатывает trailing `}` и escaped quotes)

### Безопасность
- **SSRF guard** в скрапере (env `SCRAPER_ALLOW_PRIVATE_IPS=1` для отключения в dev)
- **Prompt injection mitigation** через делимитеры пользовательского контента
- **Response size limit** 10 MB защищает от ram-exhaustion на больших страницах
- Все секреты вынесены в `.env` (`.gitignore` исключает `.env` и `data/*.sqlite3`)

## [1.1.0] — Исходная версия
Базовая версия от автора: Streamlit UI, SQLite storage, router LLM, scraper, 4 провайдера через OpenAI-совместимый API.
