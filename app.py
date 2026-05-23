from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from modules.crm import CRM_PROVIDERS, get_configured_crms
from modules.dadata import DaDataClient
from modules.gsheets import GoogleSheetsExporter
from modules.pipeline import LeadPipeline, parse_manual_rows
from modules.router import MultiProviderRouter
from modules.scraper import PublicScraper
from modules.storage import LeadStorage

load_dotenv()

st.set_page_config(page_title="Lead AI Scraper v1.3", layout="wide", page_icon="🎯")


def run(coro):
    return asyncio.run(coro)


@st.cache_resource(show_spinner=False)
def get_router(config_path: str) -> MultiProviderRouter:
    return MultiProviderRouter(config_path=config_path)


@st.cache_resource(show_spinner=False)
def get_storage(db_path: str) -> LeadStorage:
    return LeadStorage(db_path=db_path)


def get_pipeline(
    config_path: str,
    db_path: str,
    respect_robots: bool,
    per_domain_delay: float,
    use_combined_prompt: bool,
    use_prefilter: bool,
    use_dadata: bool,
    fulfillment_mode: bool = False,
) -> LeadPipeline:
    router = get_router(config_path)
    scraper = PublicScraper(respect_robots=respect_robots, per_domain_delay_sec=per_domain_delay)
    storage = get_storage(db_path)
    dadata = DaDataClient() if use_dadata else DaDataClient(api_key="")  # disabled
    return LeadPipeline(
        router=router,
        scraper=scraper,
        storage=storage,
        dadata=dadata,
        use_combined_prompt=use_combined_prompt,
        use_prefilter=use_prefilter,
        fulfillment_mode=fulfillment_mode,
    )


# ════════════════════════════════════════════════════════════════════════
# Sidebar
# ════════════════════════════════════════════════════════════════════════
st.title("🎯 Lead AI Scraper v1.3")
st.caption(
    "Поиск B2B-лидов, парсинг открытых контактов, AI-обогащение и черновики сообщений. "
    "Без авторассылки и обхода защит. Compliance-first."
)

with st.sidebar:
    st.header("⚙️ Настройки")
    config_path = st.selectbox(
        "Конфиг моделей",
        ["configs/config_default.yaml", "configs/config_ru_first.yaml"],
        index=0,
        help="default — foreign-first (Groq/Anthropic/OpenAI/Gemini); ru_first — GigaChat/YandexGPT первыми",
    )
    db_path = st.text_input("SQLite база", value=os.getenv("SCRAPER_DB", "data/leads.sqlite3"))
    respect_robots = st.toggle("Соблюдать robots.txt", value=True, help="Не отключайте на production")
    per_domain_delay = st.slider("Пауза на домен, сек", 0.5, 10.0, 1.0, 0.5)
    concurrency = st.slider("Параллельность", 1, 10, 3)

    st.divider()
    st.subheader("🤖 AI-пайплайн")
    use_combined_prompt = st.toggle(
        "Combined prompt (1 LLM-вызов вместо 3)",
        value=True,
        help="Экономит токены и время: validate+extract+score одним запросом.",
    )
    use_prefilter = st.toggle(
        "Prefilter (дешёвой моделью)",
        value=False,
        help="Быстро отсекает страницы без контактов.",
    )
    use_dadata = st.toggle(
        "DaData enrichment (ИНН/ОГРН/адрес)",
        value=bool(os.getenv("DADATA_API_KEY")),
        help="Требует DADATA_API_KEY в .env",
    )
    fulfillment_mode = st.toggle(
        "🚚 Фулфилмент-режим (Shop-logistics)",
        value=False,
        help=(
            "Использует специализированный prompt для фулфилмент-оператора. "
            "Заполняет логистический подпрофиль: маркетплейсы, объёмы заказов, "
            "категории товаров, регионы, требования (холод/маркировка ЧЗ). "
            "Скоринг подходящих лидов 0-10 (fulfillment_fit_score). "
            "Несовместим с обычным combined-prompt — переопределяет его."
        ),
    )
    if fulfillment_mode:
        st.info("🚚 Активен фулфилмент-режим — извлекаем логистический профиль")

    st.divider()
    st.caption("Минимальный AI-ключ:")
    st.code("GROQ_API_KEY=...", language="bash")
    st.caption("Ключи задаются в .env, не в интерфейсе.")

pipeline = get_pipeline(
    config_path, db_path, respect_robots, per_domain_delay,
    use_combined_prompt, use_prefilter, use_dadata, fulfillment_mode,
)
storage = get_storage(db_path)

# ════════════════════════════════════════════════════════════════════════
# Tabs
# ════════════════════════════════════════════════════════════════════════
tab_discover, tab_analyze, tab_leads, tab_providers, tab_integrations, tab_dashboard = st.tabs(
    ["1. 🔍 Поиск", "2. 📝 Анализ", "3. 📋 Лиды", "4. 🤖 Провайдеры", "5. 🔌 Интеграции", "6. 📊 Дашборд"]
)

# ────────────────────────────────────────────────────────────────────────
# 1. Поиск
# ────────────────────────────────────────────────────────────────────────
with tab_discover:
    st.subheader("Стартовый скрейпер поиска лидов")
    col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
    with col1:
        query = st.text_input("Что искать", value="интернет-магазины автозапчастей")
    with col2:
        city = st.text_input("Город", value="Москва")
    with col3:
        niche = st.text_input("Ниша", value="интернет-магазин")
    with col4:
        limit = st.number_input("Лимит", 1, 200, 20)
    seed_text = st.text_area("Seed-страницы каталогов, по одной ссылке в строке", height=120)

    if st.button("🔍 Найти лиды", type="primary"):
        seed_urls = [x.strip() for x in seed_text.splitlines() if x.strip()]
        with st.spinner("Ищу кандидатов..."):
            candidates = run(
                pipeline.discover(query=query, city=city, niche=niche, seed_urls=seed_urls, limit=int(limit))
            )
        st.session_state["candidates"] = [c.model_dump(mode="json") for c in candidates]
        st.success(f"Найдено кандидатов: {len(candidates)}")

    candidates_data = st.session_state.get("candidates", [])
    if candidates_data:
        st.dataframe(pd.DataFrame(candidates_data), use_container_width=True)
        if st.button("🚀 Анализировать найденные"):
            from modules.schemas import SearchCandidate

            candidates = [SearchCandidate(**item) for item in candidates_data]
            with st.spinner("Парсю сайты и обогащаю лиды через AI..."):
                leads, stats = run(pipeline.process_many(candidates, save=True, concurrency=int(concurrency)))
            st.session_state["last_leads"] = [l.model_dump(mode="json") for l in leads]
            st.success(
                f"Готово: {len(leads)} лидов · обогащено: {stats.enriched} · сохранено: {stats.saved} · ошибок: {len(stats.errors)}"
            )
            if stats.errors:
                with st.expander(f"⚠️ Ошибки ({len(stats.errors)})"):
                    for err in stats.errors[:10]:
                        st.warning(err)

# ────────────────────────────────────────────────────────────────────────
# 2. Анализ (ручной список)
# ────────────────────────────────────────────────────────────────────────
with tab_analyze:
    st.subheader("Ручной список компаний")
    manual = st.text_area(
        "Формат: Компания;Сайт;Город;Ниша;Заметка",
        value="Компания;Сайт;Город;Ниша;Заметка\nРомашка;https://example.com;Москва;интернет-магазин;тестовый лид",
        height=220,
    )
    if st.button("🚀 Анализировать ручной список", type="primary"):
        rows = parse_manual_rows(manual)
        if not rows:
            st.error("Не найдено строк для анализа")
        else:
            with st.spinner("Парсю сайты и обогащаю лиды..."):
                leads, stats = run(pipeline.process_many(rows, save=True, concurrency=int(concurrency)))
            st.session_state["last_leads"] = [l.model_dump(mode="json") for l in leads]
            st.success(
                f"Готово: {len(leads)} · сохранено: {stats.saved} · ошибок: {len(stats.errors)} · дубликатов: {stats.skipped}"
            )
            if stats.errors:
                with st.expander(f"⚠️ Ошибки ({len(stats.errors)})"):
                    for err in stats.errors[:10]:
                        st.warning(err)

    last = st.session_state.get("last_leads", [])
    if last:
        st.subheader("Последний результат")
        for item in last:
            enr = item.get("enrichment", {})
            with st.expander(f"{item.get('company')} — {item.get('site')}"):
                c1, c2 = st.columns(2)
                with c1:
                    st.write("**Контакт:**", item.get("contact") or "—")
                    st.write("**Тег:**", enr.get("lead_tag"))
                    st.write("**ЛПР:**", enr.get("decision_maker_name") or enr.get("decision_maker_role"))
                    st.write("**Боль:**", enr.get("pain"))
                    st.write("**УТП:**", enr.get("utp"))
                with c2:
                    st.write("**ИНН:**", item.get("inn") or "—")
                    st.write("**ОГРН:**", item.get("ogrn") or "—")
                    st.write("**Приоритет:**", enr.get("priority"))
                    st.write("**Скоринг:**", enr.get("score"))
                    if item.get("limitations"):
                        st.write("**⚠️ Limitations:**", ", ".join(item["limitations"]))
                st.text_area(
                    "Первое сообщение",
                    value=enr.get("first_message", ""),
                    height=150,
                    key=f"msg_{item.get('site')}",
                )

# ────────────────────────────────────────────────────────────────────────
# 3. Лиды
# ────────────────────────────────────────────────────────────────────────
with tab_leads:
    st.subheader("Сохранённые лиды")
    leads = storage.list_leads(limit=1000)
    rows = storage.to_csv_rows(leads)
    df = pd.DataFrame(rows)
    if not df.empty:
        # Фильтры
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            status_filter = st.multiselect("Статус", options=sorted(df["Статус"].dropna().unique()))
        with c2:
            tag_filter = st.multiselect("Тег", options=sorted(df["Тег"].dropna().unique()))
        with c3:
            priority_filter = st.multiselect("Приоритет", options=sorted(df["Приоритет"].dropna().unique()))
        with c4:
            min_score = st.number_input("Мин. скоринг", 0, 100, 0)

        df_filtered = df.copy()
        if status_filter:
            df_filtered = df_filtered[df_filtered["Статус"].isin(status_filter)]
        if tag_filter:
            df_filtered = df_filtered[df_filtered["Тег"].isin(tag_filter)]
        if priority_filter:
            df_filtered = df_filtered[df_filtered["Приоритет"].isin(priority_filter)]
        df_filtered = df_filtered[df_filtered["Скоринг"] >= min_score]

        st.write(f"Показано: **{len(df_filtered)}** из **{len(df)}**")
        st.dataframe(df_filtered, use_container_width=True, height=500)
        csv = df_filtered.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "⬇️ Скачать CSV",
            data=csv,
            file_name=f"leads_{len(df_filtered)}.csv",
            mime="text/csv",
            type="primary",
        )

        # Сохраняем отфильтрованные id для интеграций
        st.session_state["filtered_lead_sites"] = df_filtered["Сайт"].tolist()
        st.caption("Рассылку делайте вручную после проверки 50-100 лидов. Автоотправки в MVP нет.")

        # Топ-фулфилмент-лиды (только если есть лиды с логистическим профилем)
        top_log = storage.find_top_fulfillment_leads(min_fit=7, limit=50)
        if top_log:
            with st.expander(f"🚚 Топ-{len(top_log)} логистических лидов (fit_score ≥ 7)", expanded=False):
                top_rows = []
                for lead in top_log:
                    log = lead.enrichment.logistics
                    top_rows.append({
                        "Компания": lead.company,
                        "Сайт": lead.site,
                        "Fit": log.fulfillment_fit_score,
                        "Категории": ", ".join(log.product_categories),
                        "Маркетплейсы": ", ".join(log.marketplaces),
                        "Объём/мес": log.monthly_orders_range,
                        "Регионы": ", ".join(log.primary_regions),
                        "Боль": log.logistics_pain[:80],
                        "Email": lead.emails[0] if lead.emails else "",
                    })
                st.dataframe(pd.DataFrame(top_rows), use_container_width=True)
                top_csv = pd.DataFrame(top_rows).to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "⬇️ Топ-логистика CSV", data=top_csv,
                    file_name=f"top_fulfillment_{len(top_log)}.csv", mime="text/csv",
                )
                # Сохраняем для интеграций
                st.session_state["top_fulfillment_sites"] = [l.site for l in top_log]
    else:
        st.info("Пока лидов нет. Перейдите на вкладку 1 или 2.")

# ────────────────────────────────────────────────────────────────────────
# 4. Провайдеры
# ────────────────────────────────────────────────────────────────────────
with tab_providers:
    st.subheader("🤖 AI-провайдеры и лимиты")
    status = pipeline.router.provider_status()
    df_status = pd.DataFrame(status)
    if not df_status.empty:
        df_status["✅"] = df_status["configured"].map({True: "🟢", False: "⚪"})
        df_status["caps"] = df_status["capabilities"].apply(lambda x: ", ".join(x))
        cols_to_show = ["✅", "name", "model", "format", "caps", "priority", "rpm", "rpd", "used_today", "enabled"]
        st.dataframe(df_status[cols_to_show], use_container_width=True, hide_index=True)
        configured_count = int(df_status["configured"].sum())
        total_count = len(df_status)
        st.metric(
            "Сконфигурировано / всего",
            f"{configured_count} / {total_count}",
            help="Достаточно одного работающего провайдера, остальные — fallback.",
        )
        if configured_count == 0:
            st.error("❌ Не найдено ни одного API-ключа. Заполните .env и перезапустите.")
        else:
            st.success(f"✅ Готово к работе. Активных моделей: {configured_count}.")

    st.divider()
    st.subheader("Поддерживаемые форматы API")
    st.markdown(
        """
        | Формат | Провайдеры |
        |---|---|
        | `openai` | OpenAI, Groq, OpenRouter, DeepSeek, Mistral, Together AI, xAI Grok, Gemini-OpenAI bridge |
        | `anthropic` | Claude (Haiku, Sonnet, Opus) |
        | `gemini-native` | Google AI Studio v1beta (резерв) |
        | `yandex` | YandexGPT (foundation models) |
        | `gigachat` | Sberbank GigaChat (auto-refresh OAuth) |
        """
    )

# ────────────────────────────────────────────────────────────────────────
# 5. Интеграции
# ────────────────────────────────────────────────────────────────────────
with tab_integrations:
    st.subheader("🔌 Интеграции")

    # ── DaData ─────────────────────────────────────────────────────────
    st.markdown("### 🏛️ DaData (обогащение реквизитов)")
    dd = DaDataClient()
    if dd.is_configured:
        st.success("✅ DADATA_API_KEY задан. Включите тогл в сайдбаре, чтобы обогащать лиды.")
        with st.expander("🔎 Проверить компанию по ИНН"):
            test_inn = st.text_input("ИНН", placeholder="7707083893")
            if st.button("Найти") and test_inn:
                result = run(dd.find_by_inn(test_inn))
                if result:
                    st.json(result)
                else:
                    st.warning("Не найдено.")
    else:
        st.info("⚪ DaData выключена. Добавьте `DADATA_API_KEY` в `.env` (бесплатные 10 000/день на dadata.ru).")

    st.divider()

    # ── Google Sheets ───────────────────────────────────────────────────
    st.markdown("### 📊 Google Sheets")
    exporter = GoogleSheetsExporter()
    if exporter.is_configured:
        st.success(f"✅ Google Sheets настроен (режим: **{exporter.mode}**)")
        spreadsheet_id = st.text_input(
            "Spreadsheet ID (для service-account)",
            help="ID из URL: https://docs.google.com/spreadsheets/d/<ID>/edit",
            placeholder="1A2b3C... (только если режим service_account)",
        )
        worksheet_name = st.text_input("Лист", value="Leads")
        if st.button("⬆️ Экспортировать все лиды в Sheets"):
            all_leads = storage.list_leads(limit=10000)
            all_rows = storage.to_csv_rows(all_leads)
            with st.spinner(f"Отправляю {len(all_rows)} строк..."):
                result = run(exporter.export(all_rows, spreadsheet_id=spreadsheet_id or None, worksheet_name=worksheet_name))
            if result.get("ok"):
                st.success(f"✅ Готово: {result}")
                if result.get("url"):
                    st.markdown(f"[Открыть таблицу]({result['url']})")
            else:
                st.error(f"❌ {result}")
    else:
        st.info(
            "⚪ Google Sheets выключен. Варианты подключения:\n"
            "- `GOOGLE_SERVICE_ACCOUNT_JSON` — путь к JSON или сам JSON service-account (`pip install gspread google-auth`)\n"
            "- `GSHEETS_WEBHOOK_URL` — простой Apps Script webhook"
        )

    st.divider()

    # ── CRM ─────────────────────────────────────────────────────────────
    st.markdown("### 🔌 CRM (webhook-экспорт)")
    configured_crms = get_configured_crms()
    if configured_crms:
        st.success(f"✅ Сконфигурировано CRM: **{', '.join(configured_crms.keys())}**")
        crm_choice = st.selectbox("CRM для экспорта", list(configured_crms.keys()))
        filter_choice = st.radio(
            "Что отправлять",
            [
                "Только отфильтрованные с вкладки 'Лиды'",
                "Все лиды",
                "Только draft_ready",
                "🚚 Топ-фулфилмент (fit_score≥7)",
            ],
            horizontal=True,
        )
        if st.button(f"📤 Отправить в {crm_choice}", type="primary"):
            all_leads = storage.list_leads(limit=10000)
            if filter_choice == "Только отфильтрованные с вкладки 'Лиды'":
                sites = set(st.session_state.get("filtered_lead_sites", []))
                target = [l for l in all_leads if l.site in sites]
            elif filter_choice == "Только draft_ready":
                target = [l for l in all_leads if l.status == "draft_ready"]
            elif filter_choice.startswith("🚚"):
                target = storage.find_top_fulfillment_leads(min_fit=7, limit=500)
            else:
                target = all_leads
            crm = configured_crms[crm_choice]
            with st.spinner(f"Отправляю {len(target)} лидов..."):
                if hasattr(crm, "push_many"):
                    result = run(crm.push_many(target))
                else:
                    results = []
                    for lead in target:
                        results.append(run(crm.push(lead)))
                    result = {
                        "total": len(results),
                        "succeeded": sum(1 for r in results if r.get("ok")),
                        "failed": [i for i, r in enumerate(results) if not r.get("ok")],
                    }
            st.success(f"✅ Готово: {result}")
    else:
        st.info(
            "⚪ Ни одна CRM не сконфигурирована. Доступные интеграции:\n"
            "- **Generic webhook** (`CRM_WEBHOOK_URL`) — для n8n, Make, Zapier\n"
            "- **Shop-logistics / Fulfillment** (`SHOP_LOGISTICS_WEBHOOK_URL`) — плоский payload с логистическим профилем\n"
            "- **Bitrix24** (`BITRIX24_WEBHOOK_URL`)\n"
            "- **amoCRM** (`AMOCRM_SUBDOMAIN` + `AMOCRM_ACCESS_TOKEN`)\n"
            "- **HubSpot** (`HUBSPOT_ACCESS_TOKEN`)"
        )

# ────────────────────────────────────────────────────────────────────────
# 6. Дашборд
# ────────────────────────────────────────────────────────────────────────
with tab_dashboard:
    st.subheader("📊 Дашборд")
    s = storage.stats()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Всего лидов", s["total"])
    c2.metric("С email", s["with_email"])
    c3.metric("С ИНН", s["with_inn"])
    c4.metric(
        "Draft ready",
        s["by_status"].get("draft_ready", 0),
        help="Лиды с готовым первым сообщением",
    )

    st.divider()
    st.subheader("По статусам")
    if s["by_status"]:
        df_st = pd.DataFrame([{"Статус": k, "Количество": v} for k, v in s["by_status"].items()])
        st.bar_chart(df_st.set_index("Статус"))
    else:
        st.info("Лидов пока нет.")

    st.divider()
    st.subheader("По тегам")
    leads_all = storage.list_leads(limit=10000)
    if leads_all:
        tag_counts: dict[str, int] = {}
        for l in leads_all:
            tag_counts[l.enrichment.lead_tag or "не определено"] = tag_counts.get(l.enrichment.lead_tag or "не определено", 0) + 1
        df_tags = pd.DataFrame([{"Тег": k, "Количество": v} for k, v in tag_counts.items()])
        st.bar_chart(df_tags.set_index("Тег"))

st.divider()
st.caption(
    "🛡️ Compliance: только открытые данные, robots.txt, SSRF-guard, лимиты, "
    "ручная проверка и черновики сообщений без массовой авторассылки."
)
