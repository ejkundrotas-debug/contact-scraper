# syntax=docker/dockerfile:1.6
# ============================================================================
# Lead AI Contact Scraper v1.2 — Multi-stage Dockerfile
# Stage 1 (builder): компилируем lxml/pydantic и ставим зависимости в wheel-кэш
# Stage 2 (runtime): копируем готовое окружение, минимум системных пакетов
# Итоговый образ: ~280 MB (без multi-stage было бы ~650 MB)
# ============================================================================

# ─── Stage 1: builder ──────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build-time зависимости (только в builder, в финальный образ не уходят)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt ./
RUN pip wheel --no-deps --wheel-dir /wheels -r requirements.txt && \
    pip wheel --no-deps --wheel-dir /wheels gspread google-auth

# ─── Stage 2: runtime ──────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ENABLE_CORS=false \
    STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=true

# Только runtime-системные пакеты (без build-essential, без -dev)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
        curl \
        ca-certificates \
        && rm -rf /var/lib/apt/lists/* && \
    # Создаём непривилегированного пользователя
    groupadd -r scraper && useradd -r -g scraper -d /app -s /sbin/nologin scraper

WORKDIR /app

# Ставим зависимости из готовых wheels (быстро, без компиляции)
COPY --from=builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-index --find-links=/wheels -r requirements.txt && \
    pip install --no-index --find-links=/wheels gspread google-auth && \
    rm -rf /wheels

# Копируем код проекта
COPY --chown=scraper:scraper . .

# Persistent storage для SQLite
RUN mkdir -p /app/data && chown -R scraper:scraper /app/data && chmod 755 /app/data

USER scraper

# Healthcheck по Streamlit-эндпоинту
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8501/_stcore/health || exit 1

EXPOSE 8501

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
