#!/usr/bin/env bash
# macOS LaunchAgent 安装脚本：把服务注册为开机自启的后台任务
#
#   ./scripts/install_macos_launchd.sh install     # 安装并启动
#   ./scripts/install_macos_launchd.sh status      # 查看状态
#   ./scripts/install_macos_launchd.sh restart     # 重启
#   ./scripts/install_macos_launchd.sh uninstall   # 卸载
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.moss.live"
PORT="${PORT:-8000}"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${ROOT}/logs"
VENV_PY="${ROOT}/.venv/bin/python"

usage() { echo "用法: $0 {install|uninstall|restart|status|logs}"; exit 1; }
[ $# -ge 1 ] || usage

ensure_plist() {
  mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"
  cat > "${PLIST}" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${VENV_PY}</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>app.main:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>${PORT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/moss-live.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/moss-live.err.log</string>
</dict>
</plist>
PLIST_EOF
}

case "${1:-}" in
  install)
    if [ ! -x "${VENV_PY}" ]; then
      echo "!! 未找到虚拟环境，请先执行 ./scripts/run.sh 安装依赖"; exit 1
    fi
    ensure_plist
    launchctl bootout "gui/${UID}/${LABEL}" 2>/dev/null || true
    if ! launchctl bootstrap "gui/${UID}" "${PLIST}"; then
      {
        echo "!! 注册失败。常见于受限会话（launchctl 返回 5: Input/output error）。"
        echo "   plist 已经写到：${PLIST}"
        echo "   注意：这个文件留在 LaunchAgents 目录里，下次登录时系统仍会自动加载它，"
        echo "         等于未经确认就开机自启。不想这样就先卸掉："
        echo "           ./scripts/install_macos_launchd.sh uninstall"
        echo "   想启用的话，在普通终端里执行一次："
        echo "           launchctl bootstrap gui/\$(id -u) '${PLIST}'"
      } >&2
      exit 1
    fi
    launchctl enable "gui/${UID}/${LABEL}"
    echo "已安装并启动：http://127.0.0.1:${PORT}"
    echo "日志：${LOG_DIR}/moss-live.log"
    ;;
  uninstall)
    launchctl bootout "gui/${UID}/${LABEL}" 2>/dev/null || true
    rm -f "${PLIST}"
    echo "已卸载 ${LABEL}"
    ;;
  restart)
    launchctl kickstart -k "gui/${UID}/${LABEL}"
    echo "已重启"
    ;;
  status)
    launchctl print "gui/${UID}/${LABEL}" 2>/dev/null | sed -n '1,20p' || echo "未安装"
    ;;
  logs)
    tail -f "${LOG_DIR}/moss-live.log" "${LOG_DIR}/moss-live.err.log"
    ;;
  *)
    usage
    ;;
esac
