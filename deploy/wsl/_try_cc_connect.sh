#!/usr/bin/env bash
# _try_cc_connect.sh — 启动 cc-connect 8 秒看能否连飞书；之后自动退出。
# 如果看到 "connected to wss://" 说明飞书侧 WebSocket 联通成功。
# 看到 "InvalidAppId" / "InvalidAppSecret" / "InvalidEventSubscription" 等错误说明
# 飞书开放平台侧权限/订阅没配齐。
# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"
NODE_BIN="$(ccp_resolve_private_node)" || {
    echo "[ERR] 项目私有 Node.js 24.20.0 缺失或完整性校验失败" >&2
    exit 1
}
CC_CONNECT_BIN="${CHATCOPILOT_CC_CONNECT_BIN:-$HOME/.local/share/agentstrata/node-tools/cc-connect-1.4.0-beta.3/node_modules/.bin/cc-connect}"
CC_CONNECT_BIN="$(readlink -f "$CC_CONNECT_BIN")"
export PATH="$(dirname "$NODE_BIN"):$PATH"
echo "--- cc-connect version ---"
"$NODE_BIN" "$CC_CONNECT_BIN" --version 2>&1 | head -3
echo
echo "--- starting cc-connect (8s, then auto-stop) ---"
timeout --signal=INT 8 "$NODE_BIN" "$CC_CONNECT_BIN" 2>&1 || true
echo
echo "--- (cc-connect was stopped automatically after 8s) ---"
