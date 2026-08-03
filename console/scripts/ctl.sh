#!/usr/bin/env bash
# ctl.sh — 控制台「启动 / 停止 / 重启」按钮背后的唯一 sh 入口。
#
# 包一层 systemctl --user <verb> chatcopilot@<id>，把 user manager 必需的
# XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS 补好（控制台后端自身也跑成服务时，
# 非登录会话里这两个变量可能缺失）。
#
# 用法（WSL 终端 / 控制台后端）：
#   bash ctl.sh start   lingye-copilot-qq
#   bash ctl.sh stop    lingye-copilot-qq
#   bash ctl.sh restart lingye-copilot-qq
#
# 退出码：透传 systemctl 的退出码；参数非法返回 2。
set -uo pipefail

VERB="${1:-}"
INSTANCE="${2:-}"

case "$VERB" in
    start|stop|restart) ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "[ERR] 不支持的动作：'$VERB'（仅 start|stop|restart）" >&2; exit 2 ;;
esac

if [ -z "$INSTANCE" ]; then
    echo "[ERR] 缺少实例 id：bash ctl.sh $VERB <instance-id>" >&2
    exit 2
fi

uid="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$uid}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$uid/bus}"

UNIT="chatcopilot@${INSTANCE}.service"
echo "[ctl] systemctl --user $VERB $UNIT"
exec systemctl --user "$VERB" "$UNIT"
