# Архитектурный план: Lead AI Scraper на Cloudflare

## TL;DR

Чистый Cloudflare Pages **не подойдёт** для всего проекта — CPU-лимиты (10-30мс/запрос), отсутствие filesystem и невозможность держать долгие HTTP-запросы с задержками per-domain делают скрейпинг невозможным. Решение — **гибрид**:

```
┌─────────────────────────────────────────────────────────────┐
│  CLOUDFLARE EDGE                                            │
│  ┌──────────────────┐   ┌─────────────────────────────┐    │
│  │ Pages (Hono UI)  │←→│ Queue (Cloudflare Queues)   │    │
│  │ - login/auth     │   │ scrape-jobs                 │    │
│  │ - dashboard      │   └──────────────┬──────────────┘    │
│  │ - leads list     │                  │                   │
│  │ - CSV export     │                  ▼                   │
│  │ - LLM provider   │   ┌──────────────────────────────┐   │
│  │   status         │   │ D1 SQLite (leads)            │   │
│  └────────┬─────────┘   │ KV (cache, usage counters)   │   │
│           │             │ R2 (HTML snapshots, exports) │   │
│           │             └──────────────────────────────┘   │
└───────────┼─────────────────────────────────────────────────┘
            │                       ↑
            │                       │
            ▼                       │
┌─────────────────────────────────────────────────────────────┐
│  VPS / Hetzner / DigitalOcean (1 vCPU, 1GB)                │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ Python worker (consumer)                             │  │
│  │ - poll queue                                         │  │
│  │ - run scrape + LLM enrichment                        │  │
│  │ - write to D1 via Cloudflare API                     │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## 1. Что переезжает на Cloudflare (Hono UI)

### Frontend
- **Streamlit → Hono + Tailwind + Alpine.js / HTMX**
  Streamlit плох для production: не масштабируется, нет аутентификации из коробки, странный state-management. Hono c SSR-рендером HTML и небольшим JS даёт более быстрый UI и встроенные edge-фичи.

### API endpoints (Hono routes)
```
POST   /api/jobs              # создать scrape-задачу → push в queue
GET    /api/jobs/:id          # статус задачи
GET    /api/leads             # список лидов (фильтры: status/tag/score/inn)
GET    /api/leads/:id         # детали лида
POST   /api/leads/:id/status  # обновить статус (manual review)
GET    /api/leads/export.csv  # стрим CSV из D1
POST   /api/integrations/sheets  # экспорт в Google Sheets (sync, лимит ~25к строк)
POST   /api/integrations/crm     # push в CRM webhook
GET    /api/providers         # статус LLM-провайдеров (KV)
```

### Хранилища
- **D1**: таблица `leads` (та же схема, что в SQLite-версии)
- **KV**: 
  - `llm_cache:{sha256}` — кэш AI-ответов (TTL 30 дней)
  - `usage:{provider}:{date}` — счётчики RPM/RPD, переживают рестарты
  - `jobs:{id}` — статусы задач
- **R2**: HTML-снэпшоты страниц для аудита, экспортные CSV-файлы (presigned URL)

### LLM-роутинг
Можно перенести **частично**: легковесные модели (Groq, Anthropic Haiku, OpenAI 4o-mini, Gemini Flash) вызываются прямо из Hono Worker — LLM-запрос < 30 секунд укладывается в Workers free-plan лимит. Тяжёлые long-context модели лучше держать в Python worker.

---

## 2. Что остаётся в Python worker (VPS)

### Почему именно VPS, а не Workers
| Задача | Workers free | Workers paid | VPS |
|---|---|---|---|
| Скрейпинг 5 страниц сайта с throttling 1с/домен | ❌ (10мс CPU) | ❌ (30мс CPU) | ✅ |
| Playwright для JS-heavy сайтов | ❌ (нет браузера) | ❌ | ✅ |
| Парсинг HTML 1MB через BeautifulSoup | ⚠️ медленно | ⚠️ | ✅ |
| Долгие LLM-запросы (60+ сек, large context) | ❌ | ✅ (но дорого) | ✅ |
| Persistent state (per-domain throttle) | ❌ | ⚠️ через DO | ✅ |

### Worker-демон
- Python скрипт-демон на VPS (`pm2`/`systemd`)
- Получает задачи через `wrangler queues consumer` API или REST poll к D1
- Делает всю тяжёлую работу: scrape → regex → LLM enrich → DaData → write to D1
- Отписывается о статусе в `jobs:{id}` через Cloudflare API

### Стоимость
- **Минимальный VPS**: Hetzner CPX11 (€4.50/мес), достаточно для 1000 лидов/день
- **Cloudflare Workers**: $5/мес Paid plan (даст 10M requests + Queues + D1 Pro)
- **LLM**: зависит от объёма, ~$10-50/мес при умеренной нагрузке

---

## 3. Поэтапный план миграции

### Этап 1 — UI на Hono (1-2 дня)
- Создать Hono Pages проект `lead-ai-scraper-ui`
- Реализовать роуты `/leads`, `/jobs`, `/providers` с read-only D1
- Сохранить функционал «Дашборд», «Лиды», «Провайдеры»
- Streamlit оставить для admin/dev режима

### Этап 2 — D1 миграция (1 день)
- `wrangler d1 create lead-ai-prod`
- Перенести schema из `storage.py` в `migrations/0001_init.sql`
- Скрипт `import_sqlite.py` для переноса существующих лидов

### Этап 3 — Queue + Worker (2-3 дня)
- `wrangler queues create scrape-jobs`
- POST /api/jobs → push в queue
- Python consumer на VPS:
  ```python
  # consumer.py
  while True:
      job = await cloudflare_api.pull_message("scrape-jobs")
      lead = await pipeline.process_candidate(job.payload)
      await cloudflare_api.d1_insert("leads", lead.dict())
      await cloudflare_api.ack(job.id)
  ```

### Этап 4 — LLM на edge (1 день)
- Лёгкие модели (validate/score) — прямо в Hono Worker
- Поделить router.py: edge-роутер + python-роутер

### Этап 5 — Интеграции (1 день)
- Google Sheets webhook → Hono API
- CRM push → Hono API
- DaData prefetch → KV кэш

---

## 4. Альтернатива: всё на VPS, Cloudflare как edge cache

Если не хочется заморачиваться с queue:
- Streamlit/FastAPI на VPS
- Cloudflare Pages как статический фронт (если разделим UI и API)
- Cloudflare Tunnel для безопасного проброса VPS API наружу без открытых портов
- Cloudflare WAF + Rate Limiting для защиты

Это **проще** и **дешевле** на старте, но не использует edge-преимущества.

---

## 5. Что я НЕ рекомендую

❌ **Полный перенос Python на Workers** — даже с Python Workers (в beta) Playwright не запустится, и тяжёлый async-pipeline упрётся в CPU.

❌ **Использовать только Durable Objects** — для долгого скрейпинга DO дорогие, и всё равно есть лимит на длительность запроса.

❌ **Перенос всех LLM-вызовов на edge** — long-context Claude/GPT-4 могут идти 30-60 секунд, что выходит за лимиты Workers без Paid plan и Cron Triggers.

---

## 6. Готовый Hono UI скелет (для старта)

Если решишь переходить — могу сразу собрать MVP UI на Hono + D1 + Tailwind за пару итераций. Скелет будет включать:
- `src/index.tsx` — Hono с роутами
- `src/d1.ts` — обёртка над D1
- `src/components/` — JSX компоненты дашборда
- `migrations/0001_leads.sql`
- `wrangler.jsonc` с D1 + KV + Queue
- `worker/` — Python consumer для VPS

Скажи слово — поднимем.
