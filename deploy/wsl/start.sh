#!/usr/bin/env bash
# start.sh — 在当前 WSL 终端前台启动一个 AgentStrata 实例。
#
# QQ/微信直接 exec Python Gateway host；本项目不再安装或启动 cc-connect。
# 保持前台 MainPID，Ctrl+C 即停。
#
# 注意：日常起停推荐走运维控制台（systemd 托管）或 console/scripts/ctl.sh；
# 本脚本是前台手动启动模式，也是 systemd 单元 chatcopilot@.service 的 ExecStart。
#
# 用法（在 WSL 终端里）：
#   bash ~/ChatCopilot/deploy/wsl/start.sh
#   bash ~/ChatCopilot/deploy/wsl/start.sh --apply-config   # legacy edge 启动前渲染配置
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
# 实例 runtime env 是权威配置源；每次启动都重新加载。Gateway QQ 直接使用
# OneBot/Gateway 配置，legacy Feishu edge 才由 cc-connect 消费平台凭据。
ccp_load_env "AGENTSTRATA_LLM_CONFIG$|FEISHU_APP_ID|FEISHU_APP_SECRET|TAVILY_API_KEY|QQ_|CHATCOPILOT_|WORKSPACE_ROOT"
ccp_apply_bot_deploy_config

MT_HOME="${CHATCOPILOT_HOME:-$CCP_HOME_DEFAULT}"
WS_ROOT="${WORKSPACE_ROOT:-${CHATCOPILOT_WORKSPACE_ROOT:-$CCP_WORKSPACE_ROOT_DEFAULT}}"
LOG_DIR="${CHATCOPILOT_LOG_DIR:-$CCP_LOG_DIR_DEFAULT}"
PY="${CHATCOPILOT_ACP_PY:-$MT_HOME/.venv/bin/python}"
BOT_SPEC="${CHATCOPILOT_BOT_SPEC:-$MT_HOME/bots/${CHATCOPILOT_INSTANCE_ID:-lingye-copilot-qq}/bot.yaml}"

if [ ! -x "$PY" ]; then
    err "项目 Python 不可执行：$PY"
    exit 127
fi
if [ ! -f "$BOT_SPEC" ]; then
    err "找不到 BotSpec：$BOT_SPEC"
    exit 1
fi

if ccp_bot_uses_gateway "$BOT_SPEC"; then
    if [ "$APPLY_CONFIG" = "1" ]; then
        step "Gateway QQ 使用已 provision 的实例 env；不渲染 cc-connect 配置"
    fi
    mkdir -p "$LOG_DIR" || {
        err "无法创建日志目录：$LOG_DIR"
        exit 1
    }
    export CHATCOPILOT_HOME="$MT_HOME"
    export CHATCOPILOT_BOT_SPEC="$BOT_SPEC"
    export CHATCOPILOT_WORKSPACE_ROOT="$WS_ROOT"
    export WORKSPACE_ROOT="$WS_ROOT"
    export PYTHONPATH="$MT_HOME/src${PYTHONPATH:+:$PYTHONPATH}"
    LEGACY_STOP="$MT_HOME/deploy/wsl/_stop_cc.sh"
    if [ ! -f "$LEGACY_STOP" ]; then
        err "缺少 legacy 进程清理脚本：$LEGACY_STOP"
        exit 1
    fi
    step "停止本实例遗留的 cc-connect/QQ Relay（如有）"
    if ! bash "$LEGACY_STOP"; then
        err "遗留 cc-connect/QQ Relay 未能安全停止；拒绝启动 Gateway，避免双重投递。"
        exit 1
    fi
    step "前台启动 AgentStrata Gateway host"
    echo "    python:    $PY"
    echo "    instance:  ${CHATCOPILOT_INSTANCE_ID:-default}"
    echo "    bot spec:  $BOT_SPEC"
    echo "    gateway:   ws://127.0.0.1:${CHATCOPILOT_GATEWAY_PORT:-18789}"
    echo "    provider:  ${CHATCOPILOT_QQ_ONEBOT_WS_URL:-ws://127.0.0.1:3001} (external NapCat/OneBot)"
    exec "$PY" -m chatcopilot.core.release_runtime \
        --control "$MT_HOME/.agentstrata-versions" --service gateway \
        --component chatcopilot --bot "$BOT_SPEC" -- \
        "$PY" -m chatcopilot run --bot "$BOT_SPEC"
fi

err "内置部署已移除 cc-connect；非 Gateway 平台不再提供一键桥接启动。"
err "请使用 QQ/微信原生 Gateway，或自行通过 ACP/Gateway 协议接入客户端。"
exit 78
