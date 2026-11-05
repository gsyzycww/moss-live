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
else
  # 目录改名/搬家后，venv 里 pip、uvicorn 的 shebang 仍指向旧解释器。
  # 直接 source activate 后调用 pip 会报 bad interpreter，这里提前交给 install.sh 重建。
  for _s in pip uvicorn; do
    [ -f "${VENV_DIR}/bin/${_s}" ] || continue
    _interp="$(head -1 "${VENV_DIR}/bin/${_s}" | sed 's/^#!//')"
    _interp="${_interp%% *}"          # 兼容 "#!/usr/bin/env python3"
    case "${_interp}" in
      /*)
        if [ ! -x "${_interp}" ]; then
          echo "==> 虚拟环境已失效（启动器指向 ${_interp}），正在重新准备"
          "${ROOT}/scripts/install.sh" || exit 1
          break
        fi
        ;;
    esac
  done
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
