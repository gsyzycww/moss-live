#!/usr/bin/env bash
# MOSS Live 自愈启动脚本（幂等）
#
# 用途：在无法使用 launchd 权限、或服务意外退出时，把服务重新拉起来。
# 它被 ~/.zshrc 里的一个标记块调用，所以每次打开终端都会做一次极轻量的健康检查。
#
#   ./scripts/autostart.sh            # 确保服务在跑，打印结果并等待就绪
#   ./scripts/autostart.sh --quiet    # 供 shell 启动时调用：不打印、不等待
#   ./scripts/autostart.sh --status   # 只看状态，不改状态
#   ./scripts/autostart.sh --stop     # 停止服务（shell 钩子仍在，下次开终端会重新拉起）
#   PORT=9000 ./scripts/autostart.sh  # 指定端口
#
# 退出码：0 = 运行中/已启动，1 = 启动失败，2 = 端口被他人占用，64 = 参数错误
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

QUIET=0
STOP=0
STATUS_ONLY=0
case "${1:-}" in
  --quiet) QUIET=1 ;;
  --stop) STOP=1 ;;
  --status) STATUS_ONLY=1 ;;
  "") ;;
  *) echo "未知参数：$1（可用 --quiet / --status / --stop）" >&2; exit 64 ;;
esac

STATE_DIR="${ROOT}/logs"
LOG_FILE="${STATE_DIR}/moss-live.log"
LOCK_FILE="${STATE_DIR}/.autostart.lock"
mkdir -p "${STATE_DIR}"

say() { [ "${QUIET}" = "1" ] || echo "$@"; }
warn() { echo "$@" >&2; }

# --noproxy 必须加：用户环境里常有 HTTP_PROXY，健康检查绕代理会变慢甚至误判
healthy() {
  curl -sf --noproxy '*' -m 0.6 -o /dev/null "http://${HOST}:${PORT}/api/health" 2>/dev/null
}

listen_pids() {
  lsof -nP -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true
}

# ---------- --status：只报告 ----------
if [ "${STATUS_ONLY}" = "1" ]; then
  if healthy; then
    echo "运行中：http://${HOST}:${PORT}（pid $(listen_pids | head -1)）"
    exit 0
  fi
  echo "未运行：http://${HOST}:${PORT}"
  exit 1
fi

# ---------- --stop：先 TERM，5 秒不退再 KILL ----------
if [ "${STOP}" = "1" ]; then
  pids="$(listen_pids)"
  rm -f "${LOCK_FILE}"
  if [ -z "${pids}" ]; then
    say "服务未运行，无需停止"
    exit 0
  fi
  for pid in ${pids}; do kill -TERM "${pid}" 2>/dev/null || true; done
  for _ in $(seq 1 25); do
    healthy || break
    sleep 0.2
  done
  if healthy; then
    for pid in ${pids}; do kill -KILL "${pid}" 2>/dev/null || true; done
    say "已强制停止（端口 ${PORT}）"
  else
    say "已停止（端口 ${PORT}）"
  fi
  exit 0
fi

# ---------- 1) 已在运行：直接返回（最常见，开销约 3ms） ----------
if healthy; then
  say "MOSS Live 已在运行：http://${HOST}:${PORT}"
  exit 0
fi

# ---------- 2) 端口被别的进程占着：不抢、也不谎报成功 ----------
if [ -n "$(listen_pids)" ]; then
  warn "端口 ${PORT} 已被其他进程占用，未启动 MOSS Live"
  exit 2
fi

# ---------- 3) 并发保护：多个终端同时打开时只让一个去启动 ----------
# 用 PID 文件锁而非目录锁：持锁进程已死则视为陈旧锁，可自动接管。
take_lock() {
  # 兼容早期版本遗留的目录型锁
  [ -d "${LOCK_FILE}" ] && rmdir "${LOCK_FILE}" 2>/dev/null || true
  if ( set -o noclobber; echo "$$" >"${LOCK_FILE}" ) 2>/dev/null; then
    return 0
  fi
  holder="$(cat "${LOCK_FILE}" 2>/dev/null || true)"
  if [ -n "${holder}" ] && kill -0 "${holder}" 2>/dev/null; then
    return 1
  fi
  rm -f "${LOCK_FILE}"
  ( set -o noclobber; echo "$$" >"${LOCK_FILE}" ) 2>/dev/null
}
if ! take_lock; then
  exit 0
fi
cleanup() { rm -f "${LOCK_FILE}"; }
trap cleanup EXIT INT TERM

# ---------- 4) 依赖自检 ----------
if [ ! -x "${ROOT}/.venv/bin/python" ]; then
  warn "找不到 ${ROOT}/.venv/bin/python，请先执行 ./scripts/run.sh 完成首次安装"
  exit 1
fi
[ -f "${ROOT}/.env" ] || warn "缺少 .env（可从 .env.example 复制并填入 MOSS_API_KEY）"

# ---------- 5) 启动：脱离当前会话，日志追加写入 logs/moss-live.log ----------
echo "===== $(date '+%Y-%m-%d %H:%M:%S') autostart =====" >>"${LOG_FILE}"
nohup "${ROOT}/.venv/bin/python" -m uvicorn app.main:app \
  --host "${HOST}" --port "${PORT}" >>"${LOG_FILE}" 2>&1 &
child=$!

# 静默模式（终端启动时调用）不等待，避免拖慢终端
if [ "${QUIET}" = "1" ]; then
  exit 0
fi

for _ in $(seq 1 40); do
  if healthy; then
    say "MOSS Live 已启动：http://${HOST}:${PORT}（pid ${child}，日志 logs/moss-live.log）"
    exit 0
  fi
  sleep 0.5
done

warn "启动超时（20 秒），请查看 logs/moss-live.log"
exit 1
