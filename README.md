# Lead AI Contact Scraper v1.2

Локальное Streamlit-приложение для легального B2B-лидогенератора с расширенной AI-роутингом, обогащением через ДаДата, экспортом в Google Sheets и интеграциями с CRM.

**Что нового в v1.2:**
- ✅ Critical bug fixes: SSRF-guard, WAL-mode SQLite, корректная AI-валидация, точная оценка токенов с учётом кириллицы
- ✅ Combined-prompt mode (1 LLM-вызов вместо 3 — экономия 60% токенов)
- ✅ GigaChat OAuth auto-refresh + правильные адаптеры для YandexGPT, Anthropic Claude, Gemini-native
- ✅ Новые провайдеры: **OpenAI GPT-4o**, **Anthropic Claude**, **DeepSeek**, **Mistral**, **Together AI**, **xAI Grok**
- ✅ Извлечение ИНН/ОГРН/КПП + дедупликация по реквизитам
- ✅ Обогащение через **ДаДата** (юр. название, адрес, ОКВЭД, ФИО директора)
- ✅ Экспорт в **Google Sheets** (service-account или webhook)
- ✅ Интеграции с CRM: **Bitrix24, amoCRM, HubSpot, Generic webhook** (для Shop-logistics / n8n / Make / Zapier)
- ✅ Дашборд со статистикой и фильтрами
- ✅ Docker + docker-compose для VPS-деплоя
- ✅ 14 unit-тестов покрывают критичные пути

---

## 🚀 Быстрый запуск

### Локально (Python)

```bash
unzip contact_scraper_v1_2.zip
cd webapp

python3.11 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# Заполните минимум GROQ_API_KEY или любой другой LLM-ключ

set -a; source .env; set +a
streamlit run app.py
```

Откройте http://localhost:8501

### Через Docker

```bash
cp .env.example .env
# Заполните ключи

docker compose up -d --build
docker compose logs -f scraper
```

UI: http://localhost:8501

---

## 🔑 Минимальный набор ключей

Достаточно ОДНОГО ключа из любого провайдера:

```env
# Бесплатные tier-ы
GROQ_API_KEY=...                # https://console.groq.com (рекомендуется для старта)
OPENROUTER_API_KEY=...          # https://openrouter.ai
GEMINI_API_KEY=...              # https://aistudio.google.com

# Премиум (платные)
ANTHROPIC_API_KEY=...           # https://console.anthropic.com
OPENAI_API_KEY=...              # https://platform.openai.com
DEEPSEEK_API_KEY=...            # https://platform.deepseek.com (очень дёшево)
MISTRAL_API_KEY=...             # https://console.mistral.ai
TOGETHER_API_KEY=...            # https://api.together.xyz
XAI_API_KEY=...                 # https://x.ai/api

# Российские
GIGACHAT_AUTH_KEY=...           # auto-refresh; https://developers.sber.ru/gigachat
YANDEXGPT_API_KEY=...
YANDEX_FOLDER_ID=...
```

**Опциональные ключи для расширенной функциональности:**
```env
DADATA_API_KEY=...              # https://dadata.ru (10 000/день бесплатно)
BRAVE_SEARCH_API_KEY=...        # https://api.search.brave.com
SERPER_API_KEY=...              # https://serper.dev
TAVILY_API_KEY=...              # https://tavily.com

GOOGLE_SERVICE_ACCOUNT_JSON=... # путь к JSON или сам JSON

CRM_WEBHOOK_URL=...             # для Shop-logistics / n8n / Make / Zapier
BITRIX24_WEBHOOK_URL=...
AMOCRM_SUBDOMAIN=...
AMOCRM_ACCESS_TOKEN=...
HUBSPOT_ACCESS_TOKEN=...
```

---

## 📂 Структура проекта

```
webapp/
├── app.py                       # Streamlit UI (6 вкладок)
├── requirements.txt
├── pytest.ini
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── .dockerignore
├── AUDIT.md                     # Подробный аудит проекта
├── CLOUDFLARE_ARCHITECTURE.md   # План миграции на Cloudflare
├── README.md
├── configs/
│   ├── config_default.yaml      # foreign-first (Groq/Anthropic/OpenAI первыми)
│   ├── config_ru_first.yaml     # RU-first (GigaChat/YandexGPT первыми)
│   └── free-models.yaml         # capability map
├── modules/
│   ├── __init__.py
│   ├── schemas.py               # Pydantic v2 модели + regex
│   ├── prompts.py               # системные/extract/combined/score промпты
│   ├── router.py                # MultiProviderRouter (5 форматов API)
│   ├── scraper.py               # PublicScraper (robots, SSRF, INN extract)
│   ├── discovery.py             # Brave/Serper/Tavily search
│   ├── storage.py               # SQLite WAL + INN/OGRN dedupe
│   ├── pipeline.py              # LeadPipeline (combined/sequential mode)
│   ├── dadata.py                # DaData enrichment
│   ├── gsheets.py               # Google Sheets export
│   └── crm.py                   # Bitrix24/amoCRM/HubSpot/Generic webhook
├── scripts/
│   ├── gigachat_auth.py         # ручное получение GigaChat-токена
│   └── check_project.py         # smoke-test
├── tests/
│   └── test_schemas_router.py   # 14 тестов
└── data/
    └── leads.sqlite3            # WAL-mode SQLite (создаётся автоматически)
```

---

## 🧭 Как пользоваться

### Вкладка 1. 🔍 Поиск
Введите запрос, город, нишу. С ключами Brave/Serper/Tavily — ищет автоматически. Без ключей — указывайте seed-страницы каталогов в текстовом поле, приложение соберёт ссылки оттуда.

### Вкладка 2. 📝 Анализ
Вставьте список вручную:
```
Компания;Сайт;Город;Ниша;Заметка
Ромашка;https://example.com;Москва;интернет-магазин;тестовый лид
```

### Вкладка 3. 📋 Лиды
- Все сохранённые лиды с фильтрами (статус, тег, приоритет, мин. скоринг)
- Экспорт CSV (UTF-8 BOM для Excel)

### Вкладка 4. 🤖 Провайдеры
- Статус всех LLM-провайдеров (configured, RPM, RPD, использовано сегодня)
- Поддерживаемые форматы API

### Вкладка 5. 🔌 Интеграции
- DaData: проверка компаний по ИНН
- Google Sheets: экспорт всех лидов в таблицу
- CRM: push отфильтрованных лидов в Bitrix24/amoCRM/HubSpot/webhook

### Вкладка 6. 📊 Дашборд
- Метрики: всего лидов, с email, с ИНН, draft_ready
- Графики по статусам и тегам

---

## 🧠 AI-пайплайн

```
сайт компании
   │
   ├─→ SSRF-guard (блокирует localhost/private IP)
   │
   ├─→ robots.txt check (опционально)
   │
   ├─→ HTML fetch (главная + контактные страницы, throttling per-domain)
   │
   ├─→ regex-extract (emails / phones / Telegram / соцсети / ИНН / ОГРН / КПП)
   │
   ├─→ DaData enrich (опционально, по ИНН → юр.имя, адрес, директор)
   │
   ├─→ Prefilter (опционально, дешёвая модель отсеивает страницы без контактов)
   │
   ├─→ AI enrichment:
   │     • Combined mode (по умолчанию): 1 LLM-вызов = validate + extract + score
   │     • Sequential mode (legacy):     3 раздельных вызова
   │
   ├─→ Dedupe по ИНН → ОГРН → site (помечает лид limitations=[duplicate_by_inn])
   │
   └─→ SQLite WAL upsert
```

### Поддерживаемые форматы LLM API

| Формат | Кто | Auto-refresh |
|---|---|---|
| `openai` | OpenAI, Groq, OpenRouter, DeepSeek, Mistral, Together, xAI, Gemini-bridge | — |
| `anthropic` | Claude 3.5 Haiku/Sonnet/Opus | — |
| `gemini-native` | Google AI Studio v1beta | — |
| `yandex` | YandexGPT (foundation models) | — |
| `gigachat` | Sberbank GigaChat | ✅ через `GIGACHAT_AUTH_KEY` |

---

## 🔐 Безопасность и compliance

**Сделано:**
- ✅ Только открытые HTML-страницы (без обхода авторизации/капчи)
- ✅ robots.txt включён по умолчанию
- ✅ Per-domain throttle + lock
- ✅ SSRF-guard: блокировка localhost / 10.x / 192.168.x / 169.254.x / IPv6 loopback
- ✅ Лимит размера ответа (10 MB) для защиты от OOM
- ✅ Защита от prompt injection: текст сайта в `<<<USER_CONTENT_START>>>...` блоках
- ✅ Не кэшируем «отрицательные» AI-ответы (cache poisoning через scraped content)
- ✅ Soft-validation телефонов и email (порог ≥10 цифр, blacklist `example.com`)
- ✅ SQL-параметризация, `eval()` не используется
- ✅ JSON-поля хранятся через `json.dumps`/`json.loads`, не `pickle`/`eval`
- ✅ Нет массовой авторассылки — только черновики для ручной проверки

**Не сделано (технический долг):**
- Персистентный usage-counter (сейчас in-memory, ресет при рестарте)
- Авторизация пользователей в UI
- Очередь задач (Celery/RQ) для асинхронного скрейпинга
- Корневой сертификат «Минцифры» для GigaChat TLS

---

## 🧪 Тесты и проверка проекта

```bash
# Smoke-test (без сетевых вызовов)
python scripts/check_project.py

# Unit-тесты (14 шт.)
python -m pytest tests/ -v

# Компиляция всех модулей
python -m compileall modules/ scripts/ app.py
```

Что тестируется:
- Нормализация телефонов / email / blacklist
- JSON-парсинг из markdown с trailing garbage
- Оценка токенов с учётом кириллицы
- SSRF-блокировка private IP
- Извлечение ИНН/ОГРН/КПП
- SQLite WAL + дедупликация по ИНН
- Fallback enrichment без LLM
- Корректное отключение DaData/CRM без ключей

---

## 🐳 Docker

```bash
docker compose up -d --build
docker compose logs -f scraper
docker compose down
```

Что внутри:
- `python:3.11-slim` + lxml + gspread + healthcheck
- `data/` примонтирован как volume — SQLite переживает рестарты
- `.env` подхватывается через `env_file`
- Healthcheck по `/_stcore/health`
- Опционально: раскомментировать `caddy` сервис для HTTPS-проксирования

---

## 🔧 Скрипты

```bash
# Получение GigaChat access token вручную (если auto-refresh не работает)
export GIGACHAT_AUTH_KEY="base64-of-client-id:client-secret"
python scripts/gigachat_auth.py

# Полная проверка проекта
python scripts/check_project.py
```

---

## 🎯 Рекомендованный первый тест

1. Заполните только `GROQ_API_KEY` (бесплатно на console.groq.com)
2. `streamlit run app.py`
3. Перейдите во вкладку «2. Анализ»
4. Вставьте 3-5 реальных компаний:
   ```
   Wildberries;https://www.wildberries.ru;Москва;e-commerce;маркетплейс
   Ozon;https://www.ozon.ru;Москва;e-commerce;маркетплейс
   ```
5. Включите тогл «Combined prompt» (по умолчанию вкл.)
6. Нажмите «Анализировать»
7. Проверьте УТП и first_message во вкладке «Лиды»
8. После проверки качества — подключайте Brave/Tavily и DaData для масштаба

---

## 🛣️ Дорожная карта

### v1.3 (планируется)
- [ ] Персистентный usage-store в SQLite/KV
- [ ] Batch extraction для `extract_batch` capability (Groq Scout / Claude Haiku)
- [ ] OAuth-логин в Streamlit
- [ ] Очередь задач (Redis-backed)
- [ ] Кэш HTML-снэпшотов с TTL

### v2.0 (на Cloudflare)
- См. `CLOUDFLARE_ARCHITECTURE.md` — гибридная архитектура Hono UI + VPS worker

---

## 📞 Чем могу помочь дальше

- Поднять Cloudflare Pages-MVP (Hono UI + D1)
- Подключить твою CRM Shop-logistics напрямую (нужен webhook URL/swagger)
- Добавить кастомные ICP-промпты под твою нишу (fulfillment, e-commerce)
- Написать ETL для импорта существующих лидов из Excel в SQLite
- Настроить supervisor/systemd на твоём VPS
- Добавить мониторинг (Prometheus + Grafana) для usage-counter'ов

---

## ⚖️ Лицензия / условия

Только для законной работы с открытыми данными компаний. Не используйте для:
- Обхода robots.txt / капчи / авторизации
- Массовой авторассылки без согласия получателей
- Сбора персональных данных вне публикуемой компанией информации
- Нарушения локального законодательства о персональных данных (152-ФЗ, GDPR)

Авторы инструмента не несут ответственности за нецелевое использование.
