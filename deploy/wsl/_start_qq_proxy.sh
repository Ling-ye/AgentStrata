#!/usr/bin/env bash
# _start_qq_proxy.sh — 启动【当前实例】的 QQ OneBot @ Relay。
#
# QQ 实例固定启动；非 QQ 实例直接 no-op 退出 0。
# 由 start.sh 在 `exec cc-connect` 之前调用：
#   - 成功（或非 QQ）→ 退出 0；
#   - Relay 起不来（端口占用 / 崩溃）→ 退出 3，阻止 QQ 实例启动；
#   - OneBot token / 回环地址 / 双向认证探针失败 → 退出 4，阻止 QQ 实例启动。
#
# 进程：后台 nohup 跑 `python -m chatcopilot qq-at-proxy`，pidfile/日志按实例隔离。
set -uo pipefail

# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"
ccp_prepend_user_bins
ccp_apply_bot_deploy_config
ccp_load_env "QQ_ACCOUNT|QQ_WS_URL|QQ_ACCESS_TOKEN|QQ_AT_PROXY_URL|QQ_REQUIRE_AT_IN_GROUP|QQ_AT_ALL_COUNTS|CHATCOPILOT_|WORKSPACE_ROOT"
ccp_apply_bot_deploy_config

MT_HOME="${CHATCOPILOT_HOME:-$CCP_HOME_DEFAULT}"
LOG_DIR="${CHATCOPILOT_LOG_DIR:-$CCP_LOG_DIR_DEFAULT}"
CC_HOME="${CHATCOPILOT_CC_HOME:-$CCP_CC_HOME_DEFAULT}"
CC_CONFIG_DIR="${CHATCOPILOT_CC_CONNECT_CONFIG_DIR:-$CC_HOME/.cc-connect}"
CC_CONFIG="$CC_CONFIG_DIR/config.toml"

PIDFILE="$CC_HOME/qq-at-proxy.pid"
PROXY_LOG_DIR="$LOG_DIR/qq-at-proxy"
PROXY_LOG="$PROXY_LOG_DIR/$(date +%F).log"
INSTANCE_ID="${CHATCOPILOT_INSTANCE_ID:-}"

log() { printf "[qq-at-proxy] %s\n" "$*" >&2; }

process_has_env() {
    local pid="$1" assignment="$2"
    tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
        | awk -v expected="$assignment" '$0 == expected { found = 1 } END { exit(found ? 0 : 1) }'
}

relay_pid_matches_instance() {
    local pid="${1:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    [ "$(stat -c '%u' "/proc/$pid" 2>/dev/null || true)" = "$(id -u)" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null \
        | awk '
            previous_two == "-m" && previous_one == "chatcopilot" \
                && $0 == "qq-at-proxy" { found = 1 }
            { previous_two = previous_one; previous_one = $0 }
            END { exit(found ? 0 : 1) }
        ' \
        || return 1
    process_has_env "$pid" "CHATCOPILOT_INSTANCE_ID=$INSTANCE_ID" \
        && process_has_env "$pid" "CHATCOPILOT_CC_HOME=$CC_HOME"
}

# ---------- 是否为 QQ 实例 ----------
# platform=qq 由已渲染的 config.toml 判定（最可靠）。配置缺失时不启动
# cc-connect，避免落入它自带的默认拓扑。
if [ ! -f "$CC_CONFIG" ]; then
    log "cc-connect 配置不存在：$CC_CONFIG；拒绝启动"
    exit 3
fi
if ! grep -q 'type = "qq"' "$CC_CONFIG" 2>/dev/null; then
    log "非 qq 实例，跳过 QQ @ Relay"
    exit 0
fi
if [ -z "$INSTANCE_ID" ]; then
    log "缺少 CHATCOPILOT_INSTANCE_ID；无法绑定 Relay 进程身份"
    exit 4
fi
for _legacy_key in QQ_REQUIRE_AT_IN_GROUP QQ_AT_ALL_COUNTS; do
    if [ -n "${!_legacy_key+x}" ]; then
        log "已废弃配置 $_legacy_key 仍然存在；请删除后重新 provision"
        exit 4
    fi
done

# ---------- 停旧 Relay ----------
if [ -r "$PIDFILE" ]; then
    OLD="$(tr -d ' \t\r\n' < "$PIDFILE" 2>/dev/null || true)"
    if relay_pid_matches_instance "$OLD"; then
        log "停止旧 Relay pid=$OLD"
        kill -TERM "$OLD" 2>/dev/null || true
        sleep 1
        if relay_pid_matches_instance "$OLD"; then
            kill -KILL "$OLD" 2>/dev/null || true
        fi
    elif [ -n "$OLD" ]; then
        log "忽略未绑定当前实例的残留 Relay pid=$OLD"
    fi
    rm -f -- "$PIDFILE"
fi

# ---------- 选 python ----------
VENV_PY="$MT_HOME/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    log "缺少项目锁定的 Python：$VENV_PY；拒绝回退到系统解释器"
    exit 3
fi
PY="$VENV_PY"

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

mkdir -p "$PROXY_LOG_DIR" "$CC_HOME" 2>/dev/null || true
PROXY_URL="${QQ_AT_PROXY_URL:-ws://127.0.0.1:3002}"
IFS=$'\t' read -r RELAY_HOST PORT < <(
    "$PY" -c \
        'import sys; from urllib.parse import urlsplit; parsed = urlsplit(sys.argv[1]); print(parsed.hostname or "", parsed.port or "", sep="\t")' \
        "$PROXY_URL"
)
if [ -z "$RELAY_HOST" ] || [ -z "$PORT" ]; then
    log "Relay 监听地址无法解析；拒绝启动 QQ 实例"
    exit 4
fi

log "启动 Relay：$PY -m chatcopilot qq-at-proxy（监听 $PROXY_URL，上游 ${QQ_WS_URL:-ws://127.0.0.1:3001}，日志 $PROXY_LOG）"
PYTHONPATH="$MT_HOME/src${PYTHONPATH:+:$PYTHONPATH}" \
    nohup env -u QQ_ALLOW_FROM -u QQ_ALLOW_GROUPS \
    CHATCOPILOT_INSTANCE_ID="$INSTANCE_ID" CHATCOPILOT_CC_HOME="$CC_HOME" \
    "$PY" -m chatcopilot qq-at-proxy >> "$PROXY_LOG" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PIDFILE"

# ---------- 健康门：最多等 ~10s，确认进程存活 + 端口在监听 ----------
for _ in $(seq 1 20); do
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        log "Relay 进程已退出（pid=$NEW_PID）；详见 $PROXY_LOG"
        rm -f -- "$PIDFILE"
        exit 3
    fi
    if ! relay_pid_matches_instance "$NEW_PID"; then
        sleep 0.5
        continue
    fi
    if "$PY" -c \
        'import socket, sys; connection = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=0.25); connection.close()' \
        "$RELAY_HOST" "$PORT" >/dev/null 2>&1; then
        if PYTHONPATH="$MT_HOME/src${PYTHONPATH:+:$PYTHONPATH}" \
            QQ_ACCESS_TOKEN="${QQ_ACCESS_TOKEN:-}" \
            "$PY" -m chatcopilot.platforms.qq.gateway_health \
            probe --url "$PROXY_URL" --url-env-key QQ_AT_PROXY_URL; then
            if relay_pid_matches_instance "$NEW_PID"; then
                log "Relay 就绪并通过下游认证与 OneBot 往返探针（pid=$NEW_PID，监听 $RELAY_HOST:$PORT）"
                exit 0
            fi
            log "Relay 探针完成后进程身份已变化；拒绝报告就绪"
            break
        fi
        log "Relay 下游认证或 OneBot 往返探针失败；拒绝启动 QQ 实例"
        break
    fi
    sleep 0.5
done

log "Relay 在 ~10s 内未就绪（$RELAY_HOST:$PORT 未监听）；详见 $PROXY_LOG"
if relay_pid_matches_instance "$NEW_PID"; then
    kill -TERM "$NEW_PID" 2>/dev/null || true
fi
sleep 1
if relay_pid_matches_instance "$NEW_PID"; then
    kill -KILL "$NEW_PID" 2>/dev/null || true
fi
rm -f -- "$PIDFILE"
exit 3
