#!/usr/bin/env bash
# 一键安装：在任意 macOS / Linux 机器上把 MOSS Live 跑起来
#
#   ./scripts/install.sh              # 装依赖 + 生成 .env + 自检（不启动）
#   ./scripts/install.sh --start      # 装完直接启动（前台）
#   ./scripts/install.sh --port 9000 --start
#   ./scripts/install.sh --no-deps    # 只生成 .env 与自检，跳过依赖安装
#
# 设计原则：可反复执行（幂等），从不覆盖已有 .env，不写死任何绝对路径。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"
PORT="${PORT:-8000}"
START=0
SKIP_DEPS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --start) START=1 ;;
    --no-deps) SKIP_DEPS=1 ;;
    --port) shift; PORT="${1:-8000}" ;;
    --python) shift; PYTHON_BIN="${1:-python3}" ;;
    -h|--help)
      sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 64 ;;
  esac
  shift
done

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
info() { printf '  \033[36m·\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

echo
echo "MOSS Live 安装器"
echo "目录：${ROOT}"
echo

# ---------- 1. Python ----------
echo "[1/5] 检查 Python"
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "找不到 ${PYTHON_BIN}，请先安装 Python 3.10 或更高版本"
PY_VER="$("${PYTHON_BIN}" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
PY_OK="$("${PYTHON_BIN}" -c 'import sys;print(1 if sys.version_info[:2]>=(3,10) else 0)' 2>/dev/null)"
[ "${PY_OK}" = "1" ] || die "Python 版本过低（当前 ${PY_VER}），需要 3.10 及以上"
ok "Python ${PY_VER}（${PYTHON_BIN}）"

# ---------- 2. 虚拟环境 ----------
echo "[2/5] 准备虚拟环境 ${VENV_DIR}"

# venv 里的 pip / uvicorn 是启动器脚本，创建时就把解释器绝对路径写进了 shebang。
# 项目目录一旦改名或搬家，这些 shebang 仍指向旧路径，运行时表现为
#   bad interpreter: no such file or directory
# 而 `bin/python` 本身仍然可执行，所以「存在即复用」的旧判断会漏掉这种情况。
venv_stale() {
  [ -d "${VENV_DIR}" ] || return 1
  local s interp
  for s in pip uvicorn; do
    [ -f "${VENV_DIR}/bin/${s}" ] || continue
    head -1 "${VENV_DIR}/bin/${s}" | grep -q '^#!' || continue
    interp="$(head -1 "${VENV_DIR}/bin/${s}" | sed 's/^#!//')"
    # 只取第一个词：兼容 "#!/usr/bin/env python3" 这种写法
    [ -x "${interp%% *}" ] || return 0
  done
  return 1
}

if [ -x "${VENV_DIR}/bin/python" ] && ! venv_stale; then
  ok "已存在，复用"
else
  if [ -d "${VENV_DIR}" ]; then
    BAK="${VENV_DIR}.broken-$(date +%Y%m%d%H%M%S)"
    mv "${VENV_DIR}" "${BAK}" || die "无法移走失效的虚拟环境 ${VENV_DIR}"
    warn "旧虚拟环境已失效（启动器指向旧路径），已改名为 ${BAK}"
    info "确认新环境正常后可以删掉它：rm -rf ${BAK}"
  fi
  "${PYTHON_BIN}" -m venv "${VENV_DIR}" || die "创建虚拟环境失败（Debian/Ubuntu 可能需要 apt install python3-venv）"
  ok "已创建"
fi

# ---------- 3. 依赖 ----------
echo "[3/5] 安装依赖"
if [ "${SKIP_DEPS}" = "1" ]; then
  info "已按 --no-deps 跳过"
else
  if [ ! -f "${VENV_DIR}/.deps-installed" ] || [ requirements.txt -nt "${VENV_DIR}/.deps-installed" ]; then
    "${VENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || warn "pip 升级失败，继续"
    if "${VENV_DIR}/bin/python" -m pip install -q -r requirements.txt; then
      touch "${VENV_DIR}/.deps-installed"
      ok "依赖已安装"
    else
      die "依赖安装失败，请检查网络或代理（可试：${VENV_DIR}/bin/pip install -r requirements.txt）"
    fi
  else
    ok "依赖已是最新（requirements.txt 未变更）"
  fi
fi

# ---------- 4. 配置 ----------
echo "[4/5] 生成配置 .env"
if [ -f .env ]; then
  ok ".env 已存在，保持不变"
else
  cp .env.example .env || die "无法创建 .env"
  ok "已从 .env.example 生成 .env"
fi

# 端口与监听地址按参数调整（只改自己这两行，不动别的配置）
if [ "${PORT}" != "8000" ]; then
  if grep -q '^APP_PORT=' .env; then
    "${VENV_DIR}/bin/python" - "${PORT}" <<'PY'
import pathlib, re, sys
p = pathlib.Path(".env"); s = p.read_text(encoding="utf-8")
s = re.sub(r"^APP_PORT=.*$", f"APP_PORT={sys.argv[1]}", s, flags=re.M)
p.write_text(s, encoding="utf-8")
PY
    ok "APP_PORT 已设为 ${PORT}"
  fi
fi

KEY_LINE="$(grep '^MOSS_API_KEY=' .env 2>/dev/null | head -1)"
KEY_VAL="${KEY_LINE#MOSS_API_KEY=}"
KEY_VAL="${KEY_VAL//[[:space:]]/}"
if [ -z "${KEY_VAL}" ]; then
  warn "MOSS_API_KEY 为空 —— 页面能打开但无法通话"
  info "去 https://platform.moss.cn 申请后填入：${ROOT}/.env"
else
  ok "MOSS_API_KEY 已配置（结尾 ${KEY_VAL: -4}）"
fi

# ---------- 5. 自检 ----------
echo "[5/5] 自检"
if "${VENV_DIR}/bin/python" -c "from app.main import app" >/dev/null 2>&1; then
  ROUTES="$("${VENV_DIR}/bin/python" -c "from app.main import app;print(len(app.routes))" 2>/dev/null)"
  ok "应用可导入（${ROUTES} 条路由）"
else
  die "应用导入失败，请执行 ${VENV_DIR}/bin/python -c 'from app.main import app' 查看堆栈"
fi

if [ -f app/static/index.html ] && [ -f app/static/js/app.js ]; then
  ok "前端静态资源完整"
else
  die "缺少 app/static 下的前端文件，拷贝不完整"
fi

# ---------- 提示 ----------
echo
echo "安装完成。"
echo
if [ "${START}" = "1" ]; then
  echo "启动：http://127.0.0.1:${PORT}"
  echo
  exec "${VENV_DIR}/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
else
  echo "下一步："
  echo "  前台启动     ./scripts/run.sh"
  echo "  后台自愈启动 ./scripts/autostart.sh"
  echo "  开机自启     ./scripts/install_macos_launchd.sh install   # 仅 macOS"
  echo "  然后打开     http://127.0.0.1:${PORT}"
  echo
  echo "以上都需你手动执行。安装器只准备运行环境，不会注册任何开机自启。"
  echo
  echo "如果要从别的设备（手机/另一台电脑）访问，必须用 HTTPS，"
  echo "否则浏览器会禁止麦克风。见 docs/DEPLOY.md 第 12 节。"
fi
echo
