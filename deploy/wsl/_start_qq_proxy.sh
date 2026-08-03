#!/usr/bin/env bash
# _start_qq_proxy.sh — 启动【当前实例】的 QQ OneBot @ 过滤代理（群聊"必须@才回"）。
#
# 仅当实例 platform=qq 且 QQ_REQUIRE_AT_IN_GROUP!=false 时才启动；否则直接 no-op 退出 0。
# 由 start.sh 在 `exec cc-connect` 之前调用：
#   - 成功（或无需启动）→ 退出 0；
#   - 应启动但起不来（端口占用 / 崩溃）→ 退出 3，阻止 QQ 实例启动；
#   - OneBot token / 回环地址 / 双向认证探针失败 → 退出 4，阻止 QQ 实例启动。
#
# 进程：后台 nohup 跑 `python -m chatcopilot qq-at-proxy`，pidfile/日志按实例隔离。
set -uo pipefail

# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"
ccp_prepend_user_bins
ccp_apply_bot_deploy_config
ccp_load_env "QQ_|CHATCOPILOT_|WORKSPACE_ROOT"
ccp_apply_bot_deploy_config

MT_HOME="${CHATCOPILOT_HOME:-$CCP_HOME_DEFAULT}"
LOG_DIR="${CHATCOPILOT_LOG_DIR:-$CCP_LOG_DIR_DEFAULT}"
CC_HOME="${CHATCOPILOT_CC_HOME:-$CCP_CC_HOME_DEFAULT}"
CC_CONFIG_DIR="${CHATCOPILOT_CC_CONNECT_CONFIG_DIR:-$CC_HOME/.cc-connect}"
CC_CONFIG="$CC_CONFIG_DIR/config.toml"

PIDFILE="$CC_HOME/qq-at-proxy.pid"
PROXY_LOG_DIR="$LOG_DIR/qq-at-proxy"
PROXY_LOG="$PROXY_LOG_DIR/$(date +%F).log"

log() { printf "[qq-at-proxy] %s\n" "$*" >&2; }

# ---------- 是否需要启动 ----------
# platform=qq 由已渲染的 config.toml 判定（最可靠）；require_at 由 env 控制（默认 true）。
if [ ! -f "$CC_CONFIG" ] || ! grep -q 'type = "qq"' "$CC_CONFIG" 2>/dev/null; then
    log "非 qq 实例（或 config 未渲染），跳过 @ 过滤代理"
    exit 0
fi
REQUIRE_AT="${QQ_REQUIRE_AT_IN_GROUP:-true}"
case "$(printf '%s' "$REQUIRE_AT" | tr '[:upper:]' '[:lower:]')" in
    0|false|no|off)
        START_PROXY=0
        ;;
    *)
        START_PROXY=1
        ;;
esac

# ---------- 停旧代理 ----------
if [ -r "$PIDFILE" ]; then
    OLD="$(tr -d ' \t\r\n' < "$PIDFILE" 2>/dev/null || true)"
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
        log "停止旧代理 pid=$OLD"
        kill -TERM "$OLD" 2>/dev/null || true
        sleep 1
        kill -KILL "$OLD" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
fi

# ---------- 选 python ----------
VENV_PY="$MT_HOME/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
    PY="$VENV_PY"
else
    PY="$(command -v python3 || command -v python || true)"
fi
if [ -z "${PY:-}" ]; then
    log "未找到 python 解释器，无法启动代理"
    exit 3
fi

# ---------- 启动 ----------
UPSTREAM_URL="${QQ_WS_URL:-ws://127.0.0.1:3001}"
PYTHONPATH="$MT_HOME/src${PYTHONPATH:+:$PYTHONPATH}" \
    QQ_ACCESS_TOKEN="${QQ_ACCESS_TOKEN:-}" \
    "$PY" -m chatcopilot.platforms.qq.gateway_health \
    probe --url "$UPSTREAM_URL" --url-env-key QQ_WS_URL
_probe_rc=$?
if [ "$_probe_rc" != "0" ]; then
    log "OneBot 安全边界探针失败：$UPSTREAM_URL；拒绝启动 QQ 实例"
    exit 4
fi

if [ "$START_PROXY" = "0" ]; then
    log "QQ_REQUIRE_AT_IN_GROUP=$REQUIRE_AT；OneBot 双向认证已通过，跳过 @ 过滤代理"
    exit 0
fi

mkdir -p "$PROXY_LOG_DIR" "$CC_HOME" 2>/dev/null || true
PROXY_URL="${QQ_AT_PROXY_URL:-ws://127.0.0.1:3002}"
# 解析监听端口用于健康探测
PORT="$(printf '%s' "$PROXY_URL" | sed -n 's#.*:\([0-9][0-9]*\).*#\1#p')"
[ -z "$PORT" ] && PORT=3002

log "启动代理：$PY -m chatcopilot qq-at-proxy（监听 $PROXY_URL，上游 ${QQ_WS_URL:-ws://127.0.0.1:3001}，日志 $PROXY_LOG）"
PYTHONPATH="$MT_HOME/src${PYTHONPATH:+:$PYTHONPATH}" \
    nohup "$PY" -m chatcopilot qq-at-proxy >> "$PROXY_LOG" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PIDFILE"

# ---------- 健康门：最多等 ~10s，确认进程存活 + 端口在监听 ----------
for _ in $(seq 1 20); do
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        log "代理进程已退出（pid=$NEW_PID）；详见 $PROXY_LOG"
        rm -f "$PIDFILE"
        exit 3
    fi
    if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
        exec 3>&- 2>/dev/null || true
        log "代理就绪（pid=$NEW_PID，端口 $PORT 在监听）"
        exit 0
    fi
    sleep 0.5
done

log "代理在 ~10s 内未就绪（端口 $PORT 未监听）；详见 $PROXY_LOG"
kill -TERM "$NEW_PID" 2>/dev/null || true
sleep 1
kill -KILL "$NEW_PID" 2>/dev/null || true
rm -f "$PIDFILE"
exit 3
