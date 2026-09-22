#!/usr/bin/env bash
set -Eeuo pipefail

umask 027

log() {
    printf '[yysls-news] %s\n' "$*"
}

fail() {
    printf '[yysls-news] ERROR: %s\n' "$*" >&2
    exit 1
}

if [[ "$(id -u)" -ne 0 ]]; then
    fail "请使用 root 或 sudo 执行：sudo bash deploy/install.sh"
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd -- "${SCRIPT_DIR}/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-yysls-news}"
SERVICE_USER="${SERVICE_USER:-yysls-news}"
CONFIG_DIR="${CONFIG_DIR:-/etc/yysls-news}"
CONFIG_FILE="${CONFIG_DIR}/${SERVICE_NAME}.env"
DATA_HOME="${DATA_HOME:-/var/lib/yysls-news}"
VENV_DIR="${PROJECT_DIR}/.venv"

[[ -f "${PROJECT_DIR}/pyproject.toml" ]] || fail "未找到 pyproject.toml：${PROJECT_DIR}"
command -v apt-get >/dev/null 2>&1 || fail "当前一键脚本支持 Debian/Ubuntu，请在其他发行版上手动安装系统依赖。"

log "安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates \
    build-essential \
    git \
    python3 \
    python3-pip \
    python3-venv

if ! getent group "${SERVICE_USER}" >/dev/null; then
    groupadd --system "${SERVICE_USER}"
fi
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system \
        --gid "${SERVICE_USER}" \
        --home-dir "${DATA_HOME}" \
        --create-home \
        --shell /usr/sbin/nologin \
        "${SERVICE_USER}"
fi

install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${DATA_HOME}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
    "${PROJECT_DIR}/data" \
    "${PROJECT_DIR}/runtime" \
    "${PROJECT_DIR}/.playwright"
install -d -m 0750 -o root -g "${SERVICE_USER}" "${CONFIG_DIR}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    log "创建 Python 虚拟环境"
    python3 -m venv "${VENV_DIR}"
fi

log "安装项目依赖"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install --no-cache-dir -e "${PROJECT_DIR}"

log "安装 Playwright Chromium 及 Linux 运行依赖"
"${VENV_DIR}/bin/playwright" install-deps chromium
PLAYWRIGHT_BROWSERS_PATH="${PROJECT_DIR}/.playwright" \
    "${VENV_DIR}/bin/playwright" install chromium
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${PROJECT_DIR}/.playwright"

if [[ ! -f "${CONFIG_FILE}" ]]; then
    encryption_key="$(${VENV_DIR}/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    admin_password="$(${VENV_DIR}/bin/python -c 'import secrets; print(secrets.token_urlsafe(18))')"
    cat >"${CONFIG_FILE}" <<EOF
APP_ENV=production
DATABASE_PATH=${PROJECT_DIR}/data/yysls_news.db
APP_ENCRYPTION_KEY=${encryption_key}
ADMIN_USERNAME=admin
ADMIN_PASSWORD=${admin_password}
HOST=127.0.0.1
PORT=43100

BILIBILI_POLL_INTERVAL_SECONDS=300
YYSLS_POLL_INTERVAL_SECONDS=600
HTTP_TIMEOUT_SECONDS=20

QQBOT_API_BASE_URL=https://api.bot.qq.com
QQBOT_APP_ID=
QQBOT_APP_SECRET=
EOF
    chmod 0600 "${CONFIG_FILE}"
    chown root:root "${CONFIG_FILE}"
    log "已生成初始管理账号：admin"
    log "初始管理密码：${admin_password}"
else
    log "保留现有配置：${CONFIG_FILE}"
fi

sed \
    -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
    -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
    "${SCRIPT_DIR}/${SERVICE_NAME}.service" \
    >"/etc/systemd/system/${SERVICE_NAME}.service"
chmod 0644 "/etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

if ! systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
    journalctl -u "${SERVICE_NAME}.service" -n 50 --no-pager || true
    fail "服务启动失败"
fi

log "部署完成"
log "服务状态：systemctl status ${SERVICE_NAME}"
log "实时日志：journalctl -u ${SERVICE_NAME} -f"
log "管理地址：http://127.0.0.1:43100/"
log "QQBot AppID/AppSecret 仍需在管理页面配置"
