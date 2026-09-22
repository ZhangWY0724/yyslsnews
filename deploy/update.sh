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
    fail "请使用 root 或 sudo 执行：sudo bash deploy/update.sh"
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd -- "${SCRIPT_DIR}/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-yysls-news}"
SERVICE_USER="${SERVICE_USER:-yysls-news}"
VENV_DIR="${PROJECT_DIR}/.venv"

[[ -d "${PROJECT_DIR}/.git" ]] || fail "不是 Git 仓库：${PROJECT_DIR}"
[[ -x "${VENV_DIR}/bin/python" ]] || fail "虚拟环境不存在，请先执行 install.sh"

git_args=(-c "safe.directory=${PROJECT_DIR}" -C "${PROJECT_DIR}")

if [[ -n "$(git "${git_args[@]}" status --porcelain)" ]]; then
    fail "工作区存在未提交修改，请先处理后再更新"
fi

log "拉取仓库更新"
git "${git_args[@]}" pull --ff-only

log "更新 Python 依赖"
"${VENV_DIR}/bin/python" -m pip install --no-cache-dir -e "${PROJECT_DIR}"

log "同步 Playwright Chromium"
PLAYWRIGHT_BROWSERS_PATH="${PROJECT_DIR}/.playwright" \
    "${VENV_DIR}/bin/playwright" install chromium
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${PROJECT_DIR}/.playwright"

systemctl daemon-reload
systemctl restart "${SERVICE_NAME}.service"

if ! systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
    journalctl -u "${SERVICE_NAME}.service" -n 50 --no-pager || true
    fail "更新后服务启动失败"
fi

log "更新完成，服务已重启"
systemctl --no-pager --full status "${SERVICE_NAME}.service"
