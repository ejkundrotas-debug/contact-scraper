# syntax=docker/dockerfile:1.6
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps: libxml2-dev и libxslt1-dev нужны для lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости (кэшируем слой)
COPY requirements.txt ./
RUN pip install -r requirements.txt
# Опциональные deps для Google Sheets:
RUN pip install gspread google-auth

# Затем код
COPY . .

# Persistent volumes: data/ хранит SQLite между перезапусками.
RUN mkdir -p data && chmod 777 data

# Минимальный healthcheck по Streamlit-эндпоинту
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -sf http://localhost:8501/_stcore/health || exit 1

EXPOSE 8501

# Streamlit конфиг (отключаем телеметрию, прячем сайдбар по умолчанию выключен)
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
