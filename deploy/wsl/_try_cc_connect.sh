#!/usr/bin/env bash
# _try_cc_connect.sh — 启动 cc-connect 8 秒看能否连飞书；之后自动退出。
# 如果看到 "connected to wss://" 说明飞书侧 WebSocket 联通成功。
# 看到 "InvalidAppId" / "InvalidAppSecret" / "InvalidEventSubscription" 等错误说明
# 飞书开放平台侧权限/订阅没配齐。
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
echo "--- cc-connect version ---"
cc-connect --version 2>&1 | head -3
echo
echo "--- starting cc-connect (8s, then auto-stop) ---"
timeout --signal=INT 8 cc-connect 2>&1 || true
echo
echo "--- (cc-connect was stopped automatically after 8s) ---"
