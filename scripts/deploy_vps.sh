#!/usr/bin/env bash
# ============================================================================
# Lead AI Contact Scraper — VPS Deployment Script
# ============================================================================
# Запускается на чистом Ubuntu 22.04/24.04 VPS. Делает:
#   1. Устанавливает Docker и docker-compose-plugin (если нет)
#   2. Клонирует репозиторий
#   3. Создаёт .env из примера (нужно потом заполнить ключи!)
#   4. Билдит образ и запускает контейнер
#   5. Настраивает systemd-сервис для автостарта
#
# Использование на VPS (под root или sudo):
#   curl -fsSL https://raw.githubusercontent.com/ejkundrotas-debug/contact-scraper/main/scripts/deploy_vps.sh | sudo bash
#
# Или интерактивно:
#   wget https://raw.githubusercontent.com/ejkundrotas-debug/contact-scraper/main/scripts/deploy_vps.sh
#   sudo bash deploy_vps.sh
# ============================================================================

set -euo pipefail

REPO_URL="https://github.com/ejkundrotas-debug/contact-scraper.git"
INSTALL_DIR="/opt/contact-scraper"
SERVICE_USER="${SERVICE_USER:-scraper}"

log()  { echo -e "\033[1;34m[deploy]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m   $*"; }
err()  { echo -e "\033[1;31m[error]\033[0m  $*" >&2; }

# ─── Pre-flight ─────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    err "Запустите от root или через sudo"
    exit 1
fi

if ! grep -q "Ubuntu\|Debian" /etc/os-release; then
    warn "Скрипт тестировался на Ubuntu 22.04/24.04. Другие дистрибутивы — на свой страх и риск."
fi

# ─── 1. Docker ──────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    log "Устанавливаю Docker..."
    apt-get update
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    log "Docker установлен: $(docker --version)"
else
    log "Docker уже установлен: $(docker --version)"
fi

# ─── 2. Service user ────────────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    log "Создаю системного пользователя $SERVICE_USER..."
    useradd -r -m -d /home/$SERVICE_USER -s /bin/bash $SERVICE_USER
    usermod -aG docker $SERVICE_USER
fi

# ─── 3. Clone / pull repo ───────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
    log "Обновляю репозиторий в $INSTALL_DIR..."
    cd "$INSTALL_DIR"
    git pull --rebase
else
    log "Клонирую репозиторий в $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi
chown -R $SERVICE_USER:$SERVICE_USER "$INSTALL_DIR"

# ─── 4. .env ────────────────────────────────────────────────────────────────
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    log "Создаю .env из .env.example..."
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chown $SERVICE_USER:$SERVICE_USER "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    warn "⚠️  Откройте $INSTALL_DIR/.env и заполните хотя бы GROQ_API_KEY!"
    warn "    nano $INSTALL_DIR/.env"
fi

# ─── 5. Build & up ──────────────────────────────────────────────────────────
log "Билдю Docker-образ (это займёт 2-5 минут на первом запуске)..."
cd "$INSTALL_DIR"
sudo -u $SERVICE_USER docker compose build

log "Запускаю контейнер..."
sudo -u $SERVICE_USER docker compose up -d

# ─── 6. Systemd service ────────────────────────────────────────────────────
log "Регистрирую systemd-юнит для автозапуска..."
cat > /etc/systemd/system/contact-scraper.service <<EOF
[Unit]
Description=Lead AI Contact Scraper (Docker Compose)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
ExecReload=/usr/bin/docker compose restart
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable contact-scraper.service
log "Сервис contact-scraper.service зарегистрирован — старт через systemctl start contact-scraper"

# ─── 7. Health-check & summary ─────────────────────────────────────────────
sleep 10
if curl -sf http://127.0.0.1:8501/_stcore/health &>/dev/null; then
    log "✅ Streamlit отвечает на http://127.0.0.1:8501"
else
    warn "Streamlit ещё не готов — проверь логи: docker compose logs -f scraper"
fi

cat <<'BANNER'

╔═══════════════════════════════════════════════════════════════════════╗
║  ✅ Deployment complete!                                              ║
╠═══════════════════════════════════════════════════════════════════════╣
║  Веб-интерфейс:   http://<IP_VPS>:8501  (порт открыт только loopback) ║
║  Для внешнего доступа — настройте Caddy (Caddyfile.example)            ║
║                                                                       ║
║  Полезные команды (в /opt/contact-scraper):                           ║
║    docker compose logs -f scraper      # логи                         ║
║    docker compose restart              # перезапуск                   ║
║    docker compose pull && \                                            ║
║      docker compose up -d --build      # обновление                   ║
║    systemctl status contact-scraper    # статус автозапуска           ║
║                                                                       ║
║  Не забудь заполнить .env:                                            ║
║    sudo nano /opt/contact-scraper/.env                                ║
║    sudo systemctl restart contact-scraper                             ║
╚═══════════════════════════════════════════════════════════════════════╝

BANNER
