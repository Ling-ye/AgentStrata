#!/usr/bin/env bash
# _stop_cc.sh — 清理【当前实例】遗留的 cc-connect/QQ Relay 进程。
# 新 QQ Gateway 只在切换前调用它做精确实例清理；它不会启动 legacy 拓扑。
#
# 与早期版本的差异：
# - 不再 pkill -f cc-connect 全屏匹配，避免多实例并行时把别的实例一起干掉。
# - 优先读 ``$CC_HOME/cc-connect.pid``（start.sh 写入），辅以 kill -0 校验存活；
# - 兜底：扫描所有 cc-connect 进程，仅匹配 /proc/<pid>/environ 里 HOME=$CC_HOME 的实例。
#
# 用法：
#   bash ~/ChatCopilot-<instance>/deploy/wsl/_stop_cc.sh
#   CHATCOPILOT_CC_HOME=/path bash _stop_cc.sh
set -uo pipefail

# 加载实例配置（CC_HOME / CCP_CC_HOME_DEFAULT 等）
# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"
ccp_apply_bot_deploy_config
ccp_load_env "CHATCOPILOT_|WORKSPACE_ROOT"
ccp_apply_bot_deploy_config

CC_HOME="${CHATCOPILOT_CC_HOME:-$CCP_CC_HOME_DEFAULT}"
PIDFILE="$CC_HOME/cc-connect.pid"
INSTANCE_ID="${CHATCOPILOT_INSTANCE_ID:-}"

step() { printf "[stop] %s\n" "$*"; }
ok()   { printf "[OK] %s\n" "$*"; }
warn() { printf "[WARN] %s\n" "$*"; }
err()  { printf "[ERR] %s\n" "$*" >&2; }

process_has_env() {
    local pid="$1" assignment="$2"
    tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
        | awk -v expected="$assignment" '$0 == expected { found = 1 } END { exit(found ? 0 : 1) }'
}

pid_is_owned_and_running() {
    local pid="${1:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    [ "$(stat -c '%u' "/proc/$pid" 2>/dev/null || true)" = "$(id -u)" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

relay_pid_matches_instance() {
    local pid="${1:-}"
    pid_is_owned_and_running "$pid" || return 1
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

cc_connect_pid_matches_instance() {
    local pid="${1:-}"
    pid_is_owned_and_running "$pid" || return 1
    tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null \
        | awk 'index($0, "cc-connect") { found = 1 } END { exit(found ? 0 : 1) }' \
        || return 1
    process_has_env "$pid" "HOME=$CC_HOME" \
        && process_has_env "$pid" "CHATCOPILOT_INSTANCE_ID=$INSTANCE_ID"
}

# 先停 QQ @ Relay（若有）。pidfile 与兜底扫描都复检 argv、owner 和实例 env，
# 即使旧 pidfile 丢失也不能让同实例 Relay 与新 Gateway 并存。
QQ_PROXY_PIDFILE="$CC_HOME/qq-at-proxy.pid"
list_instance_relay_pids() {
    local pids="" pid
    if [ -r "$QQ_PROXY_PIDFILE" ]; then
        pid="$(tr -d ' \t\r\n' < "$QQ_PROXY_PIDFILE" 2>/dev/null || true)"
        if relay_pid_matches_instance "$pid"; then
            pids="$pid"
        elif [ -n "$pid" ]; then
            warn "忽略未绑定当前实例的残留 QQ Relay pid=$pid" >&2
        fi
    fi
    while IFS= read -r pid; do
        [ -z "$pid" ] && continue
        if relay_pid_matches_instance "$pid" \
            && ! echo " $pids " | grep -Fq " $pid "; then
            pids="${pids:+$pids }$pid"
        fi
    done < <(pgrep -f 'qq-at-proxy' 2>/dev/null || true)
    echo "$pids"
}

RELAY_PIDS="$(list_instance_relay_pids)"
if [ -n "$RELAY_PIDS" ]; then
    step "停止 QQ @ Relay pid=$RELAY_PIDS"
    for pid in $RELAY_PIDS; do
        relay_pid_matches_instance "$pid" && kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 1
    RELAY_PIDS="$(list_instance_relay_pids)"
    for pid in $RELAY_PIDS; do
        relay_pid_matches_instance "$pid" && kill -KILL "$pid" 2>/dev/null || true
    done
    [ -z "$RELAY_PIDS" ] || sleep 1
    RELAY_PIDS="$(list_instance_relay_pids)"
    if [ -n "$RELAY_PIDS" ]; then
        err "仍有 QQ Relay 进程未退出（实例 $CC_HOME）：$RELAY_PIDS"
        exit 1
    fi
fi
[ -f "$QQ_PROXY_PIDFILE" ] && rm -f -- "$QQ_PROXY_PIDFILE"

# 列出本实例对应的 cc-connect PID 集合（去重 + 校验存活）
list_instance_pids() {
    local pids=""
    if [ -r "$PIDFILE" ]; then
        local _p
        _p="$(tr -d ' \t\r\n' < "$PIDFILE" 2>/dev/null || true)"
        if cc_connect_pid_matches_instance "$_p"; then
            pids="$_p"
        elif [ -n "$_p" ]; then
            warn "忽略未绑定当前实例的残留 cc-connect pid=$_p" >&2
        fi
    fi
    # 兜底仍同时验证命令与实例环境，不能只凭进程名或 pidfile 发信号。
    local pid
    while IFS= read -r pid; do
        [ -z "$pid" ] && continue
        if cc_connect_pid_matches_instance "$pid" \
            && ! echo " $pids " | grep -Fq " $pid "; then
            pids="${pids:+$pids }$pid"
        fi
    done < <(pgrep -x cc-connect 2>/dev/null || pgrep -f cc-connect 2>/dev/null || true)
    echo "$pids"
}

PIDS="$(list_instance_pids)"
if [ -z "$PIDS" ]; then
    ok "无运行中的 cc-connect（实例 CC_HOME=$CC_HOME）"
    [ -f "$PIDFILE" ] && rm -f "$PIDFILE"
    exit 0
fi

step "停止 cc-connect（实例 CC_HOME=$CC_HOME, PID=$PIDS）"
for pid in $PIDS; do
    if cc_connect_pid_matches_instance "$pid"; then
        kill -TERM "$pid" 2>/dev/null || true
    fi
done
for _ in 1 2 3 4 5; do
    sleep 1
    PIDS="$(list_instance_pids)"
    [ -z "$PIDS" ] && break
done

if [ -n "$PIDS" ]; then
    warn "SIGTERM 未退干净，发送 SIGKILL：$PIDS"
    for pid in $PIDS; do
        if cc_connect_pid_matches_instance "$pid"; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done
    sleep 1
    PIDS="$(list_instance_pids)"
fi

if [ -n "$PIDS" ]; then
    err "仍有 cc-connect 进程未退出（实例 $CC_HOME）：$PIDS"
    exit 1
fi

ok "本实例 cc-connect 已退出"
[ -f "$PIDFILE" ] && rm -f "$PIDFILE"
