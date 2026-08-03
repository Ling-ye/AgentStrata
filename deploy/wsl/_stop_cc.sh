#!/usr/bin/env bash
# _stop_cc.sh — 优雅停止【当前实例】的 cc-connect（SIGTERM → 等 5s → SIGKILL）。
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

step() { printf "[stop] %s\n" "$*"; }
ok()   { printf "[OK] %s\n" "$*"; }
warn() { printf "[WARN] %s\n" "$*"; }
err()  { printf "[ERR] %s\n" "$*" >&2; }

# 先停 QQ @ 过滤代理（若有）。按实例 pidfile 隔离，不误杀别的实例。
QQ_PROXY_PIDFILE="$CC_HOME/qq-at-proxy.pid"
if [ -r "$QQ_PROXY_PIDFILE" ]; then
    _qpp="$(tr -d ' \t\r\n' < "$QQ_PROXY_PIDFILE" 2>/dev/null || true)"
    if [ -n "$_qpp" ] && kill -0 "$_qpp" 2>/dev/null; then
        step "停止 QQ @ 过滤代理 pid=$_qpp"
        kill -TERM "$_qpp" 2>/dev/null || true
        sleep 1
        kill -KILL "$_qpp" 2>/dev/null || true
    fi
    rm -f "$QQ_PROXY_PIDFILE"
fi

# 列出本实例对应的 cc-connect PID 集合（去重 + 校验存活）
list_instance_pids() {
    local pids=""
    if [ -r "$PIDFILE" ]; then
        local _p
        _p="$(tr -d ' \t\r\n' < "$PIDFILE" 2>/dev/null || true)"
        if [ -n "$_p" ] && kill -0 "$_p" 2>/dev/null; then
            pids="$_p"
        fi
    fi
    # 兜底：按 environ 里的 HOME=$CC_HOME 匹配，处理 pidfile 缺失或被覆盖的场景
    local pid environ
    while IFS= read -r pid; do
        [ -z "$pid" ] && continue
        if [ -r "/proc/$pid/environ" ]; then
            environ="$(tr '\0' '\n' < /proc/$pid/environ 2>/dev/null || true)"
            if echo "$environ" | grep -Fxq "HOME=$CC_HOME"; then
                if ! echo " $pids " | grep -Fq " $pid "; then
                    pids="${pids:+$pids }$pid"
                fi
            fi
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
    kill -TERM "$pid" 2>/dev/null || true
done
for _ in 1 2 3 4 5; do
    sleep 1
    PIDS="$(list_instance_pids)"
    [ -z "$PIDS" ] && break
done

if [ -n "$PIDS" ]; then
    warn "SIGTERM 未退干净，发送 SIGKILL：$PIDS"
    for pid in $PIDS; do
        kill -KILL "$pid" 2>/dev/null || true
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
