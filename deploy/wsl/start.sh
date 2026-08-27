#!/usr/bin/env bash
# start.sh — 在当前 WSL 终端【前台】启动 cc-connect（手动启动模式）
#
# start.sh 前台跑。日志直接打到终端，Ctrl+C 即停，关闭终端即停。
# 只要这个 WSL 终端窗口不关，进程就稳定存活。最适合手动启动场景。
#
# 注意：日常起停推荐走运维控制台（systemd 托管）或 console/scripts/ctl.sh；
# 本脚本是前台手动启动模式，也是 systemd 单元 chatcopilot@.service 的 ExecStart。
#
# 用法（在 WSL 终端里）：
#   bash ~/ChatCopilot/deploy/wsl/start.sh
#   bash ~/ChatCopilot/deploy/wsl/start.sh --apply-config   # 启动前先重新渲染所有配置
#
# 环境变量（在 ~/.bashrc 或 ~/.chatcopilot.env 里 export 即可）：
#   CHATCOPILOT_HOME    默认 $HOME/ChatCopilot
#   FEISHU_APP_ID / FEISHU_APP_SECRET   --apply-config 时需要
set -uo pipefail

APPLY_CONFIG=0
for arg in "$@"; do
    case "$arg" in
        --apply-config) APPLY_CONFIG=1 ;;
        -h|--help)
            sed -n '2,24p' "$0"
            exit 0
            ;;
        *)
            echo "[ERR] 未知参数：$arg（用 --help 看用法）" >&2
            exit 2
            ;;
    esac
done

step() { printf "\033[1;36m[%s]\033[0m %s\n" "$(date +%H:%M:%S)" "$*"; }
ok()   { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ERR]\033[0m %s\n" "$*" >&2; }

# ---------- PATH + env 兜底 ----------
# 非交互非登录 shell 不会读 .bashrc；实例配置和兼容 PATH 统一由
# _load_env.sh 的两个 helper 处理。
# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"
ccp_prepend_user_bins
ccp_apply_bot_deploy_config
# ~/.chatcopilot.env 是运行时权威配置源；每次启动都重新加载，覆盖当前 shell
# 里可能残留的旧飞书 App，确保 cc-connect 与 Python notifier 使用同一套凭证。
# QQ_ 必须在父进程加载：Relay、cc-connect 与平台 sender 都需要 OneBot token、
# 回传目标与 URL。ACP 名单只由 bot_wrapper 从 bot-local env 重新加载。
ccp_load_env "FEISHU_APP_ID|FEISHU_APP_SECRET|TAVILY_API_KEY|QQ_|CHATCOPILOT_|WORKSPACE_ROOT"
ccp_apply_bot_deploy_config

MT_HOME="${CHATCOPILOT_HOME:-$CCP_HOME_DEFAULT}"
WS_ROOT="${WORKSPACE_ROOT:-${CHATCOPILOT_WORKSPACE_ROOT:-$CCP_WORKSPACE_ROOT_DEFAULT}}"
LOG_DIR="${CHATCOPILOT_LOG_DIR:-$CCP_LOG_DIR_DEFAULT}"
CC_HOME="${CHATCOPILOT_CC_HOME:-$CCP_CC_HOME_DEFAULT}"
CC_CONFIG_DIR="${CHATCOPILOT_CC_CONNECT_CONFIG_DIR:-$CC_HOME/.cc-connect}"
CC_CONFIG="$CC_CONFIG_DIR/config.toml"
CC_CONNECT_BIN="${CHATCOPILOT_CC_CONNECT_BIN:-$HOME/.local/share/agentstrata/node-tools/cc-connect-1.4.0-beta.3/node_modules/.bin/cc-connect}"
NODE_BIN="$(ccp_resolve_private_node)" || {
    err "项目私有 Node.js 24.20.0 缺失或完整性校验失败"
    err "  请重新运行 deploy/wsl/install_wsl_env.sh；不会回退到系统 Node"
    exit 1
}

# ---------- 可选：重新渲染配置 ----------
if [ "$APPLY_CONFIG" = "1" ]; then
    step "重新渲染 cc-connect 配置"
    if [ ! -f "$MT_HOME/deploy/wsl/_apply_config.sh" ]; then
        err "找不到 _apply_config.sh：$MT_HOME/deploy/wsl/_apply_config.sh"
        exit 1
    fi
    bash "$MT_HOME/deploy/wsl/_apply_config.sh" || {
        err "_apply_config.sh 执行失败"
        exit 1
    }
    echo
fi

# ---------- 停止已有 cc-connect（仅当前实例） ----------
# 严格按实例隔离：只停止本 instance 对应 CC_HOME 下的 cc-connect 进程。
# 多实例并行场景下，同一台 WSL 可能同时运行多个不同 BotSpec，
# 不能粗暴 pkill -f cc-connect 把对方实例一起干掉。
step "检查并停止本实例已有 cc-connect 进程"
if [ -n "${CHATCOPILOT_INSTANCE_ID:-}" ]; then
    echo "    instance: $CHATCOPILOT_INSTANCE_ID"
fi
echo "    cc home:  $CC_HOME"
CHATCOPILOT_CC_HOME="$CC_HOME" bash "$(dirname "$0")/_stop_cc.sh"

# ---------- 清理 cc-connect 会话缓存 ----------
# cc-connect 会在 sessions/ 目录持久化旧对话 transcript；我们的 ACP server 每次
# 新建 session 时不复用历史，保留这些缓存只会导致切换模型后旧身份被回放。
if [ -d "$CC_CONFIG_DIR/sessions" ]; then
    rm -rf "$CC_CONFIG_DIR/sessions"
    echo "    [OK] 已清理 cc-connect sessions 缓存"
fi

# ---------- QQ 明确 @ 触发：启动 OneBot Relay ----------
# QQ 固定走 Relay；Relay 或 OneBot 安全边界不可用时 fail-closed，不提供直连降级。
QQ_RELAY_HELPER="$MT_HOME/deploy/wsl/_start_qq_proxy.sh"
if [ ! -f "$QQ_RELAY_HELPER" ]; then
    err "缺少 QQ Relay 启动脚本：$QQ_RELAY_HELPER"
    exit 1
fi
if ! bash "$QQ_RELAY_HELPER"; then
    err "QQ Relay 或 OneBot 安全边界不可用；拒绝启动 cc-connect"
    exit 1
fi

# ---------- 校验固定 cc-connect 可执行 ----------
if [ "${CC_CONNECT_BIN#/}" = "$CC_CONNECT_BIN" ] || [ ! -x "$CC_CONNECT_BIN" ]; then
    err "CHATCOPILOT_CC_CONNECT_BIN 必须是绝对且可执行的用户级 cc-connect：$CC_CONNECT_BIN"
    err "  请重新运行 deploy/wsl/install_wsl_env.sh，或显式配置项目私有 cc-connect 路径"
    exit 1
fi
CC_CONNECT_BIN="$(readlink -f "$CC_CONNECT_BIN")"
EXPECTED_CC_CONNECT_ENTRY="$HOME/.local/share/agentstrata/node-tools/cc-connect-1.4.0-beta.3/node_modules/cc-connect/run.js"
if [ "$CC_CONNECT_BIN" != "$EXPECTED_CC_CONNECT_ENTRY" ] \
    || [ ! -f "$CC_CONNECT_BIN" ] || [ -L "$CC_CONNECT_BIN" ] \
    || [ "$(stat -c '%u' "$CC_CONNECT_BIN" 2>/dev/null || true)" != "$(id -u)" ] \
    || [ "$(stat -c '%h' "$CC_CONNECT_BIN" 2>/dev/null || true)" != "1" ]; then
    err "cc-connect 入口必须是锁定的项目私有文件：$EXPECTED_CC_CONNECT_ENTRY"
    err "  请通过 deploy/wsl/install_wsl_env.sh 恢复锁定安装"
    exit 1
fi

# ---------- 前台启动 ----------
echo
step "前台启动 cc-connect（日志直接输出到本终端，Ctrl+C 停止）"
echo "    可执行文件：$CC_CONNECT_BIN"
echo "    Node.js:    $NODE_BIN"
echo "    instance:   ${CHATCOPILOT_INSTANCE_ID:-default}"
echo "    cc HOME:    ${CC_HOME}"
echo "    config:     ${CC_CONFIG}"
echo "    work_dir:   ${WS_ROOT}/default"
echo

PIDFILE="$CC_HOME/cc-connect.pid"
mkdir -p "$CC_HOME"

# ---------- cc-connect 日志按日期 + per-instance 落盘 ----------
# 早期版本：cc-connect 默认写 /tmp/cc-connect.log，单文件不滚动，多实例并行时互踩，
# 跑久了几百 MB；dump.sh 又必拷全量。
# 现在：通过 CC_LOG_FILE 环境变量把 cc-connect 的日志改写到
#   $CHATCOPILOT_LOG_DIR/cc-connect/<YYYY-MM-DD>.log
# 这里 $CHATCOPILOT_LOG_DIR 已天然 per-instance（~/chatcopilot-logs/<instance>/）。
# 同时维护两条 symlink，让控制台日志流 / status.sh / 用户自己的 tail 命令无感继续工作：
#   - $LOG_DIR/cc-connect/current.log → 今日文件
#   - /tmp/cc-connect.log              → 今日文件
# 并按 7 天清理过期日志，避免无限占盘。
LOG_CC_DIR="$LOG_DIR/cc-connect"
mkdir -p "$LOG_CC_DIR" 2>/dev/null || warn "无法创建 $LOG_CC_DIR，cc-connect 仍会落到 /tmp/cc-connect.log"
DAILY_CC_LOG="$LOG_CC_DIR/$(date +%F).log"

# 旧版本可能在 /tmp/cc-connect.log 留了个 regular file。先去掉它，再 symlink 过去。
# 注意：只在 /tmp/cc-connect.log 不是已经指向当前 DAILY_CC_LOG 的 symlink 时才动。
if [ -L /tmp/cc-connect.log ]; then
    _existing_target="$(readlink -f /tmp/cc-connect.log 2>/dev/null || echo "")"
    if [ "$_existing_target" != "$(readlink -f "$DAILY_CC_LOG" 2>/dev/null || echo "$DAILY_CC_LOG")" ]; then
        rm -f /tmp/cc-connect.log
    fi
elif [ -e /tmp/cc-connect.log ]; then
    # 旧的 regular file：备份到 daily log 末尾，避免丢历史
    if [ -d "$LOG_CC_DIR" ]; then
        cat /tmp/cc-connect.log >> "$DAILY_CC_LOG" 2>/dev/null || true
    fi
    rm -f /tmp/cc-connect.log
fi

if [ -d "$LOG_CC_DIR" ]; then
    : > "$DAILY_CC_LOG"  # touch（idempotent）
    ln -sfn "$DAILY_CC_LOG" "$LOG_CC_DIR/current.log" 2>/dev/null || true
    ln -sfn "$DAILY_CC_LOG" /tmp/cc-connect.log 2>/dev/null || true
    # 7 天保留策略：清理 LOG_CC_DIR 下 mtime > 7 天的 *.log，但保留 symlink
    find "$LOG_CC_DIR" -maxdepth 1 -type f -name "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].log" -mtime +7 -delete 2>/dev/null || true
    export CC_LOG_FILE="$DAILY_CC_LOG"
    echo "    cc log:     $DAILY_CC_LOG (compat: /tmp/cc-connect.log → 同一文件)"
fi

# 当前 shell 的 PID 在 exec 后不变（bash exec 用替换语义，cc-connect 继承同一 PID）。
# 因此先写 pidfile，再 exec，pidfile 内的值即为 cc-connect 自身 PID。
# 进程退出后 pidfile 会留下来"指向已死 PID"——_stop_cc.sh 与 status.sh 用 kill -0 校验存活。
echo $$ > "$PIDFILE"
echo "    pidfile:    $PIDFILE (pid=$$)"
echo
echo "    >>> 关闭本终端即停止服务 <<<"
echo

# 把 CC_LOG_FILE 显式注入子进程 env，cc-connect 与 ACP runtime 都靠它定位日志。
exec env -u QQ_ALLOW_FROM -u QQ_ALLOW_GROUPS -u NODE_OPTIONS -u NODE_PATH \
    -u NPM_CONFIG_USERCONFIG -u NPM_CONFIG_GLOBALCONFIG \
    HOME="$CC_HOME" PATH="$(dirname "$NODE_BIN"):$PATH" \
    CC_LOG_FILE="${CC_LOG_FILE:-/tmp/cc-connect.log}" \
    "$NODE_BIN" "$CC_CONNECT_BIN"
