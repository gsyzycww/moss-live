#!/usr/bin/env bash
# 一键启动脚本：准备虚拟环境 -> 安装依赖 -> 启动服务
#
#   ./scripts/run.sh                # 前台启动，读取 .env
#   PORT=9000 ./scripts/run.sh      # 指定端口
#   RELOAD=1 ./scripts/run.sh       # 开发模式（热重载）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

if [ ! -d "${VENV_DIR}" ]; then
  echo "==> 创建虚拟环境 ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

if [ ! -f "${VENV_DIR}/.deps-installed" ] || [ requirements.txt -nt "${VENV_DIR}/.deps-installed" ]; then
  echo "==> 安装依赖"
  pip install --upgrade pip >/dev/null
  pip install -r requirements.txt
  touch "${VENV_DIR}/.deps-installed"
fi

if [ ! -f .env ]; then
  echo "!! 未找到 .env，已从 .env.example 复制，请填入 MOSS_API_KEY"
  cp .env.example .env
fi

if grep -q '^MOSS_API_KEY=\s*$' .env 2>/dev/null; then
  echo "!! .env 中的 MOSS_API_KEY 为空，页面会提示“缺少密钥”"
fi

echo "==> 启动 http://${HOST}:${PORT}"
if [ "${RELOAD:-0}" = "1" ]; then
  exec uvicorn app.main:app --host "${HOST}" --port "${PORT}" --reload
else
  exec uvicorn app.main:app --host "${HOST}" --port "${PORT}"
fi
