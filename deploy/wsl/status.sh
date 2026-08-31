#!/usr/bin/env bash
# status.sh — 实例健康检查（Gateway host 或 legacy cc-connect edge）
#
# 用法：
#   bash status.sh         # 一次性输出
#   bash status.sh -w      # 每 5s 刷新一次（watch 模式，Ctrl+C 退出）
#   bash status.sh -w 2    # 每 2s 刷新
set -uo pipefail

WATCH=0
INTERVAL=5
INSTANCE=""
BOT_SPEC=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -w|--watch)
            WATCH=1
            if [ -n "${2:-}" ] && [[ "${2:-}" != -* ]]; then
                INTERVAL="$2"
                shift
            fi
            ;;
        --instance)
            [ "$#" -ge 2 ] || { echo "[ERR] --instance needs a value" >&2; exit 2; }
            INSTANCE="$2"
            shift
            ;;
        --bot-spec)
            [ "$#" -ge 2 ] || { echo "[ERR] --bot-spec needs a value" >&2; exit 2; }
            BOT_SPEC="$2"
            shift
            ;;
        -h|--help)
            sed -n '2,12p' "$0"
            echo "  --instance ID       resolve the registered instance explicitly"
            echo "  --bot-spec PATH     inspect this BotSpec explicitly"
            exit 0
            ;;
        *)
            echo "[ERR] unknown argument: $1" >&2
            exit 2
            ;;
    esac
    shift
done

# ---------- env / PATH 兜底（让显示更有意义） ----------
# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"
if [ -n "$INSTANCE" ]; then
    export CHATCOPILOT_INSTANCE_ID="$INSTANCE"
fi
if [ -n "$BOT_SPEC" ]; then
    case "$BOT_SPEC" in
        "~/"*) BOT_SPEC="$HOME/${BOT_SPEC#"~/"}" ;;
    esac
    export CHATCOPILOT_BOT_SPEC="$BOT_SPEC"
fi
if [ -z "${CHATCOPILOT_BOT_SPEC:-}" ] && [ -n "${CHATCOPILOT_INSTANCE_ID:-}" ]; then
    _status_candidate="$CCP_HOME_DEFAULT/bots/$CHATCOPILOT_INSTANCE_ID/bot.yaml"
    _status_registry="$HOME/.config/chatcopilot-console/$CHATCOPILOT_INSTANCE_ID.env"
    if [ -f "$_status_candidate" ]; then
        export CHATCOPILOT_BOT_SPEC="$_status_candidate"
    elif [ -r "$_status_registry" ]; then
        _status_home="$(sed -n 's/^CCP_WSL_HOME=//p' "$_status_registry" | head -n 1)"
        _status_candidate="$_status_home/bots/$CHATCOPILOT_INSTANCE_ID/bot.yaml"
        [ -f "$_status_candidate" ] && export CHATCOPILOT_BOT_SPEC="$_status_candidate"
    fi
fi
[ -n "${CHATCOPILOT_BOT_SPEC:-}" ] && ccp_apply_bot_deploy_config
if [ -z "${CHATCOPILOT_INSTANCE_ID:-}" ] || [ -z "${CHATCOPILOT_BOT_SPEC:-}" ] \
    || [ ! -f "$CHATCOPILOT_BOT_SPEC" ]; then
    echo "[ERR] 无法唯一解析实例；请传 --instance <id> 或 --bot-spec <path>" >&2
    exit 2
fi
ccp_load_env "FEISHU_APP_ID|FEISHU_APP_SECRET|TAVILY_API_KEY|QQ_|CHATCOPILOT_|WORKSPACE_ROOT"
[ -n "$INSTANCE" ] && export CHATCOPILOT_INSTANCE_ID="$INSTANCE"
[ -n "$BOT_SPEC" ] && export CHATCOPILOT_BOT_SPEC="$BOT_SPEC"
ccp_apply_bot_deploy_config
if [ -n "$INSTANCE" ] && [ "$CHATCOPILOT_INSTANCE_ID" != "$INSTANCE" ]; then
    echo "[ERR] --instance 与 BotSpec deploy.instance_id 不一致" >&2
    exit 2
fi
ccp_prepend_user_bins

# Gateway 是运行拓扑，platform 只为 legacy edge 保留。
RUNTIME_KIND="legacy"
PLATFORM_TYPE_FOR_STATUS=""
if [ -n "${CHATCOPILOT_BOT_SPEC:-}" ] && [ -f "$CHATCOPILOT_BOT_SPEC" ]; then
    if ccp_bot_uses_gateway "$CHATCOPILOT_BOT_SPEC"; then
        RUNTIME_KIND="gateway"
        PLATFORM_TYPE_FOR_STATUS="qq"
    fi
fi
if [ "$RUNTIME_KIND" = "legacy" ] && [ -n "${CHATCOPILOT_BOT_SPEC:-}" ] && [ -f "$CHATCOPILOT_BOT_SPEC" ]; then
    PLATFORM_TYPE_FOR_STATUS="$(BOT_SPEC="$CHATCOPILOT_BOT_SPEC" python3 - <<'PY'
import os
from pathlib import Path
bot = Path(os.environ.get("BOT_SPEC", "")).expanduser()
if not bot.is_file():
    print(""); raise SystemExit(0)
current = ""
for raw in bot.read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw.split("#", 1)[0].rstrip()
    if not line.strip(): continue
    if not raw[:1].isspace() and ":" in line:
        current = line.split(":", 1)[0].strip()
        continue
    if current == "platform" and raw[:1].isspace() and ":" in line:
        key, value = line.split(":", 1)
        if key.strip() == "type":
            print(value.strip().strip("\"'"))
            raise SystemExit(0)
print("")
PY
)"
fi
if [ -z "$PLATFORM_TYPE_FOR_STATUS" ]; then
    echo "[ERR] BotSpec 未声明可识别的 Gateway Channel 或 platform.type：$CHATCOPILOT_BOT_SPEC" >&2
    exit 2
fi

# cc-connect 主日志查找顺序：CC_LOG_FILE → $LOG_DIR/cc-connect/<date>.log →
# $LOG_DIR/cc-connect/current.log → /tmp/cc-connect.log（legacy）
LOG_DIR="${CHATCOPILOT_LOG_DIR:-$CCP_LOG_DIR_DEFAULT}"
_resolve_cc_log_for_status() {
    if [ -n "${CC_LOG_FILE:-}" ]; then
        echo "$CC_LOG_FILE"; return
    fi
    local today_log="$LOG_DIR/cc-connect/$(date +%F).log"
    [ -e "$today_log" ] && { echo "$today_log"; return; }
    local current_link="$LOG_DIR/cc-connect/current.log"
    [ -e "$current_link" ] && { echo "$current_link"; return; }
    echo "/tmp/cc-connect.log"
}
CC_LOG="$(_resolve_cc_log_for_status)"
CC_HOME="${CHATCOPILOT_CC_HOME:-$CCP_CC_HOME_DEFAULT}"
CC_CONFIG_DIR="${CHATCOPILOT_CC_CONNECT_CONFIG_DIR:-$CC_HOME/.cc-connect}"
CC_CONF="$CC_CONFIG_DIR/config.toml"
CC_CONNECT_BIN="${CHATCOPILOT_CC_CONNECT_BIN:-$HOME/.local/share/agentstrata/node-tools/cc-connect-1.4.0-beta.3/node_modules/.bin/cc-connect}"
CURSOR_MCP="$HOME/.cursor/mcp.json"
MT_HOME="${CHATCOPILOT_HOME:-$CCP_HOME_DEFAULT}"
WS_ROOT="${WORKSPACE_ROOT:-${CHATCOPILOT_WORKSPACE_ROOT:-$CCP_WORKSPACE_ROOT_DEFAULT}}"

_status_bot_requires_code_worker() {
    local python_bin="$MT_HOME/.venv/bin/python"
    if [ ! -x "$python_bin" ]; then
        python_bin="$(command -v python3 || true)"
    fi
    if [ -z "$python_bin" ] || ! "$python_bin" -c "import yaml" >/dev/null 2>&1; then
        return 1
    fi
    CHATCOPILOT_STATUS_BOT_SPEC="$CHATCOPILOT_BOT_SPEC" "$python_bin" - <<'PY'
import os
from pathlib import Path

import yaml

path = Path(os.environ["CHATCOPILOT_STATUS_BOT_SPEC"])
data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
if not isinstance(data, dict):
    raise SystemExit(f"invalid BotSpec mapping: {path}")
tools = data.get("tools") or {}
if not isinstance(tools, dict):
    raise SystemExit(f"invalid tools mapping in BotSpec: {path}")
packs = tools.get("packs") or []
if not isinstance(packs, list) or not all(
    isinstance(pack, str) for pack in packs
):
    raise SystemExit(f"invalid tools.packs in BotSpec: {path}")
print("1" if "dev.code_tasks" in packs else "0")
PY
}

CODE_WORKER_REQUIRED="unknown"
if _resolved_code_worker_required="$(_status_bot_requires_code_worker 2>/dev/null)"; then
    case "$_resolved_code_worker_required" in
        0|1) CODE_WORKER_REQUIRED="$_resolved_code_worker_required" ;;
    esac
fi

bold() { printf "\n\033[1;34m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m⚠\033[0m %s\n" "$*"; }
bad()  { printf "  \033[1;31m✗\033[0m %s\n" "$*"; }
dim()  { printf "      \033[2m%s\033[0m\n" "$*"; }

human_age() {
    local age_s="$1"
    if [ "$age_s" -lt 60 ]; then echo "${age_s}s"
    elif [ "$age_s" -lt 3600 ]; then echo "$((age_s / 60))m"
    elif [ "$age_s" -lt 86400 ]; then echo "$((age_s / 3600))h"
    else echo "$((age_s / 86400))d"
    fi
}

_status_gateway_pid_matches_instance() {
    local pid="${1:-}" expected_python expected_bot
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    [ "$(stat -c '%u' "/proc/$pid" 2>/dev/null || true)" = "$(id -u)" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    expected_python="$(readlink -f "$MT_HOME/.venv/bin/python" 2>/dev/null || true)"
    expected_bot="$MT_HOME/bots/$CHATCOPILOT_INSTANCE_ID/bot.yaml"
    [ -n "$expected_python" ] \
        && [ "$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)" = "$expected_python" ] \
        || return 1
    mapfile -d '' -t _status_gateway_argv < "/proc/$pid/cmdline" || return 1
    [ "${#_status_gateway_argv[@]}" -eq 6 ] \
        && [ "${_status_gateway_argv[1]}" = "-m" ] \
        && [ "${_status_gateway_argv[2]}" = "chatcopilot" ] \
        && [ "${_status_gateway_argv[3]}" = "run" ] \
        && [ "${_status_gateway_argv[4]}" = "--bot" ] \
        && [ "${_status_gateway_argv[5]}" = "$expected_bot" ] \
        && _status_process_has_env "$pid" "CHATCOPILOT_INSTANCE_ID=$CHATCOPILOT_INSTANCE_ID"
}

_status_probe_onebot() {
    local python_bin="$MT_HOME/.venv/bin/python"
    [ -x "$python_bin" ] || return 1
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$MT_HOME/src${PYTHONPATH:+:$PYTHONPATH}" \
        QQ_ACCESS_TOKEN="${QQ_ACCESS_TOKEN:-}" \
        "$python_bin" -m chatcopilot.platforms.qq.gateway_health \
        probe --url "${CHATCOPILOT_QQ_ONEBOT_WS_URL:-ws://127.0.0.1:3001}" \
        --url-env-key CHATCOPILOT_QQ_ONEBOT_WS_URL >/dev/null 2>&1
}

print_gateway_status() {
    local unit="chatcopilot@${CHATCOPILOT_INSTANCE_ID}.service" pid="" state="unknown"
    printf "\033[1m▣ AgentStrata Gateway 状态 — instance=%s — %s\033[0m\n" \
        "$CHATCOPILOT_INSTANCE_ID" "$(date '+%F %T')"

    bold "▶ Gateway host（systemd MainPID）"
    if command -v systemctl >/dev/null 2>&1; then
        state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
        pid="$(systemctl --user show "$unit" --property=MainPID --value 2>/dev/null || true)"
    fi
    if [ "$state" = "active" ] && _status_gateway_pid_matches_instance "$pid"; then
        ok "$unit active；MainPID=$pid 为当前实例 Python Gateway host"
        dim "$(ps -o args= -p "$pid" 2>/dev/null | sed 's/^ *//')"
    else
        bad "$unit 未形成可验证的实例 Gateway MainPID（state=${state:-unknown}, pid=${pid:-0}）"
        dim "查看：journalctl --user -u $unit -n 100"
    fi

    bold "▶ Gateway 配置证据"
    if [[ "${CHATCOPILOT_GATEWAY_PORT:-}" =~ ^[0-9]{1,5}$ ]] \
        && [ "$((10#$CHATCOPILOT_GATEWAY_PORT))" -ge 1 ] \
        && [ "$((10#$CHATCOPILOT_GATEWAY_PORT))" -le 65535 ]; then
        ok "Gateway listener = ws://127.0.0.1:$CHATCOPILOT_GATEWAY_PORT（BotSpec 固定 loopback）"
    else
        bad "CHATCOPILOT_GATEWAY_PORT 缺失或无效"
    fi
    if [[ "${CHATCOPILOT_GATEWAY_TOKEN:-}" =~ ^[A-Za-z0-9_-]{32,128}$ ]]; then
        ok "Gateway client credential 已配置（值不显示）"
    else
        bad "CHATCOPILOT_GATEWAY_TOKEN 缺失或格式无效"
    fi
    if [ -n "${CHATCOPILOT_GATEWAY_STATE_ROOT:-}" ]; then
        ok "Gateway durable state root 已配置"
        dim "CHATCOPILOT_GATEWAY_STATE_ROOT=$CHATCOPILOT_GATEWAY_STATE_ROOT"
    else
        bad "CHATCOPILOT_GATEWAY_STATE_ROOT 未配置"
    fi

    bold "▶ 外部 QQ provider（NapCat / OneBot v11）"
    dim "endpoint=${CHATCOPILOT_QQ_ONEBOT_WS_URL:-ws://127.0.0.1:3001}"
    if [ "$state" = "active" ] && _status_probe_onebot; then
        ok "OneBot 通过 token 拒绝/接受的认证只读探针"
    else
        bad "OneBot 认证只读探针未通过"
        dim "这只证明 provider 边界，不代表真实 QQ 消息、Agent、模型或客户端 E2E。"
    fi

    bold "▶ 证据边界"
    dim "active MainPID 只证明 Gateway host 进程；OneBot probe 只证明回环 provider 认证。"
    dim "未由本命令验证：真实 QQ 入站、模型调用、外部发送、用户端展示或 ACP client。"

    bold "▶ 常用命令"
    echo "  systemctl --user status $unit"
    echo "  journalctl --user -u $unit -f"
    echo "  bash $MT_HOME/deploy/wsl/qq_gateway.sh status --instance $CHATCOPILOT_INSTANCE_ID"
    echo
}

_status_process_has_env() {
    local pid="$1" assignment="$2"
    tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
        | awk -v expected="$assignment" '$0 == expected { found = 1 } END { exit(found ? 0 : 1) }'
}

_status_cc_pid_matches_instance() {
    local pid="${1:-}" node entry
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    [ "$(stat -c '%u' "/proc/$pid" 2>/dev/null || true)" = "$(id -u)" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    node="$(ccp_resolve_private_node 2>/dev/null)" || return 1
    entry="$(readlink -f "$CC_CONNECT_BIN" 2>/dev/null)" || return 1
    tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null \
        | grep -Fxq "$node" || return 1
    tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null \
        | grep -Fxq "$entry" || return 1
    _status_process_has_env "$pid" "HOME=$CC_HOME" \
        && _status_process_has_env "$pid" "CHATCOPILOT_INSTANCE_ID=$CHATCOPILOT_INSTANCE_ID"
}

_filter_instance_pids() {
    # pidfile 和扫描候选均绑定精确 Node/cc-connect 参数、owner 与实例 env。
    local pidfile="$CC_HOME/cc-connect.pid"
    local pids=""
    if [ -r "$pidfile" ]; then
        local _p
        _p="$(tr -d ' \t\r\n' < "$pidfile" 2>/dev/null || true)"
        if _status_cc_pid_matches_instance "$_p"; then
            pids="$_p"
        fi
    fi
    local pid
    while IFS= read -r pid; do
        [ -z "$pid" ] && continue
        if _status_cc_pid_matches_instance "$pid"; then
            if ! echo " $pids " | grep -Fq " $pid "; then
                pids="${pids:+$pids }$pid"
            fi
        fi
    done < <(pgrep -x cc-connect 2>/dev/null || pgrep -f cc-connect 2>/dev/null || true)
    echo "$pids"
}

print_status() {
    printf "\033[1m▣ cc-connect 健康检查 — instance=%s — %s\033[0m\n" \
        "${CHATCOPILOT_INSTANCE_ID:-default}" "$(date '+%F %T')"

    bold "▶ 进程状态（仅显示本实例 CC_HOME=$CC_HOME 下的 cc-connect）"
    INSTANCE_PIDS="$(_filter_instance_pids)"
    if [ -n "$INSTANCE_PIDS" ]; then
        local count
        count=$(echo "$INSTANCE_PIDS" | tr ' ' '\n' | grep -c .)
        ok "本实例 cc-connect 在运行（$count 个进程）"
        for pid in $INSTANCE_PIDS; do
            local etime cmd
            etime=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')
            cmd=$(ps -o args= -p "$pid" 2>/dev/null | sed 's/^ *//')
            dim "PID $pid  uptime=$etime  $cmd"
        done
    else
        bad "本实例 cc-connect 未运行"
        dim "启动：bash start.sh"
        # 顺带提示有没有其它实例在跑（帮助识别多实例并行场景）
        local total_pids total
        total_pids="$(pgrep -x cc-connect 2>/dev/null || true)"
        total="$(echo "$total_pids" | grep -c . || true)"
        if [ "${total:-0}" -gt 0 ]; then
            warn "Other cc-connect processes are running: $total (not owned by this instance)"
        fi
    fi

    bold "▶ 配置文件"
    if [ -f "$CC_CONF" ]; then
        local mtime
        mtime=$(stat -c '%y' "$CC_CONF" | cut -d. -f1)
        ok "cc-connect config: $CC_CONF"
        dim "mtime=$mtime, $(stat -c '%a' "$CC_CONF") permissions"
    else
        bad "cc-connect config 缺失：$CC_CONF"
        dim "渲染并启动：bash start.sh --apply-config"
    fi
    if [ -f "$CURSOR_MCP" ]; then
        ok "cursor MCP: $CURSOR_MCP"
    else
        warn "cursor MCP 缺失：$CURSOR_MCP（影响 cursor agent CLI 调用 MCP server）"
    fi

    bold "▶ cc-connect 主日志"
    if [ -f "$CC_LOG" ]; then
        local age_s
        age_s=$(( $(date +%s) - $(stat -c %Y "$CC_LOG") ))
        local size
        size=$(stat -c %s "$CC_LOG")
        ok "$CC_LOG ($(numfmt --to=iec --suffix=B "$size" 2>/dev/null || echo "${size}B"), 上次更新 $(human_age $age_s) 前)"

        case "$PLATFORM_TYPE_FOR_STATUS" in
            feishu)
                if grep -q "connected to wss" "$CC_LOG"; then
                    ok "WebSocket 已连接（日志里出现 'connected to wss'）"
                else
                    warn "未发现 'connected to wss' —— 飞书 WebSocket 可能没建立"
                fi
                ;;
            qq)
                if grep -qiE "connected to OneBot|qq: logged in" "$CC_LOG"; then
                    ok "OneBot 通道已连接（日志里出现 'connected to OneBot' / 'qq: logged in'）"
                else
                    warn "未在日志里发现 OneBot 连接关键词 —— 检查 NapCat 是否在跑、正向 WebSocket 是否已开"
                    if grep -q "OneBot upstream unavailable" "$CC_LOG"; then
                        bad "OneBot upstream unavailable: NapCat may not be logged in, or the OneBot WebSocket is not ready"
                        dim "Check NapCat WebUI/login and positive WebSocket port 3001, then restart this instance"
                    fi
                fi
                ;;
            *)
                dim "platform=$PLATFORM_TYPE_FOR_STATUS：未提供专门的健康标志"
                ;;
        esac

        if grep -qE "level=ERROR|panic:|\[ERR\]" "$CC_LOG"; then
            local err_count
            err_count=$(grep -cE "level=ERROR|panic:|\[ERR\]" "$CC_LOG")
            warn "日志中检测到 $err_count 条 ERROR / panic（在控制台「日志」抽屉或 grep 当日 cc-connect 日志查看）"
        fi
        if grep -qE "EACCES: permission denied.*cc-connect|Auto-install failed|/usr/lib/node_modules/cc-connect" "$CC_LOG"; then
            bad "cc-connect failed while updating a root-owned global install"
            dim "Fix: rerun deploy/wsl/install_wsl_env.sh, then restart the instance"
        fi

        echo
        printf "  \033[2m最后 5 行：\033[0m\n"
        tail -n 5 "$CC_LOG" 2>/dev/null | sed 's/^/      /'
    else
        warn "日志文件不存在：$CC_LOG（cc-connect 可能从未启动）"
    fi

    bold "▶ 环境变量（platform=$PLATFORM_TYPE_FOR_STATUS）"
    case "$PLATFORM_TYPE_FOR_STATUS" in
        feishu)
            if [ -n "${FEISHU_APP_ID:-}" ]; then
                ok "FEISHU_APP_ID = ${FEISHU_APP_ID:0:12}..."
            else
                bad "FEISHU_APP_ID 未设置（start.sh --apply-config 会失败）"
            fi
            if [ -n "${FEISHU_APP_SECRET:-}" ]; then
                ok "FEISHU_APP_SECRET = (已设置, ${#FEISHU_APP_SECRET} 字符)"
            else
                bad "FEISHU_APP_SECRET 未设置（start.sh --apply-config 会失败）"
            fi
            ;;
        qq)
            if [ -n "${QQ_WS_URL:-}" ]; then
                ok "QQ_WS_URL = $QQ_WS_URL"
            else
                dim "QQ_WS_URL 未设置（脚本将渲染为默认 ws://127.0.0.1:3001）"
            fi
            if [[ "${QQ_ACCESS_TOKEN:-}" =~ ^[A-Za-z0-9_-]{32,128}$ ]]; then
                ok "QQ_ACCESS_TOKEN = (格式有效, ${#QQ_ACCESS_TOKEN} 字符)"
            else
                bad "QQ_ACCESS_TOKEN 缺失或格式无效（必须为 32-128 位 URL-safe 字符）"
            fi
            if [ -n "${QQ_ALLOW_FROM:-}" ]; then
                ok "QQ_ALLOW_FROM = (ACP 用户准入已配置，标识已脱敏)"
            else
                warn "QQ_ALLOW_FROM 未设置（ACP 不从用户维度授予准入）"
            fi
            if [ -n "${QQ_ALLOW_GROUPS:-}" ]; then
                ok "QQ_ALLOW_GROUPS = (ACP 群准入已配置，标识已脱敏)"
            else
                dim "QQ_ALLOW_GROUPS 未设置（ACP 不从群维度授予准入）"
            fi
            ;;
        *)
            dim "platform=$PLATFORM_TYPE_FOR_STATUS：未配置凭据展示"
            ;;
    esac
    [ -n "${CHATCOPILOT_HOME:-}" ] && ok "CHATCOPILOT_HOME = $CHATCOPILOT_HOME" || dim "CHATCOPILOT_HOME 未设置（用默认 $CCP_HOME_DEFAULT）"
    [ -n "${CHATCOPILOT_INSTANCE_ID:-}" ] && ok "CHATCOPILOT_INSTANCE_ID = $CHATCOPILOT_INSTANCE_ID" || dim "CHATCOPILOT_INSTANCE_ID 未设置（default 实例）"
    [ -n "${CHATCOPILOT_BOT_SPEC:-}" ] && ok "CHATCOPILOT_BOT_SPEC = $CHATCOPILOT_BOT_SPEC" || dim "CHATCOPILOT_BOT_SPEC 未设置（按 instance 推导）"
    dim "ENV_FILE = $CCP_ENV_FILE"
    dim "CC_HOME = $CC_HOME"
    dim "CC_CONFIG = $CC_CONF"
    dim "WS_ROOT = $WS_ROOT"
    dim "MT_HOME = $MT_HOME"

    bold "▶ 关键工具"
    local private_node
    private_node="$(ccp_resolve_private_node 2>/dev/null || true)"
    if [ -n "$private_node" ]; then
        ok "Node.js 24.20.0 -> $private_node"
    else
        bad "项目私有 Node.js 24.20.0 缺失或完整性校验失败"
    fi
    if [ -n "$private_node" ] \
        && [ "${CC_CONNECT_BIN#/}" != "$CC_CONNECT_BIN" ] && [ -x "$CC_CONNECT_BIN" ]; then
        local resolved_cc
        resolved_cc="$(readlink -f "$CC_CONNECT_BIN")"
        case "$resolved_cc" in
            /usr/bin/*|/usr/local/bin/*)
                bad "cc-connect -> $resolved_cc（系统 wrapper 被禁止）"
                ;;
            *)
                ok "cc-connect -> $resolved_cc"
                local cc_version
                cc_version="$(PATH="$(dirname "$private_node"):$PATH" \
                    "$private_node" "$resolved_cc" --version 2>&1 | head -n 1)"
                dim "version=$cc_version"
                if [ "$PLATFORM_TYPE_FOR_STATUS" = "qq" ] \
                    && [[ "$cc_version" != *"1.4.0-beta.3"* ]]; then
                    bad "QQ 实例要求固定 cc-connect 1.4.0-beta.3；升级只能走显式安装流程"
                fi
                ;;
        esac
    else
        bad "cc-connect 固定路径不可执行：$CC_CONNECT_BIN"
    fi
    local tools=(rsync dos2unix)
    if [ "$PLATFORM_TYPE_FOR_STATUS" = "feishu" ]; then
        tools+=(lark-cli)
    fi
    for tool in "${tools[@]}"; do
        if command -v "$tool" >/dev/null 2>&1; then
            ok "$tool -> $(command -v $tool)"
        else
            case "$tool" in
                cc-connect) bad "$tool 未在 PATH（必装；platform=qq 需要 cc-connect@beta）" ;;
                lark-cli)   warn "$tool 未在 PATH（飞书数据下载相关命令将不可用）" ;;
                *)          warn "$tool 未在 PATH（非必须）" ;;
            esac
        fi
    done
    if [ "$CODE_WORKER_REQUIRED" = 1 ]; then
        if [ -n "${CHATCOPILOT_CODEX_BIN:-}" ] && [ -x "$CHATCOPILOT_CODEX_BIN" ]; then
            ok "Codex -> $(readlink -f "$CHATCOPILOT_CODEX_BIN")"
            dim "version=$("$CHATCOPILOT_CODEX_BIN" --version 2>&1 | head -n 1)"
        else
            warn "专用代码任务 Codex 可执行文件未配置或不可执行"
        fi
        if [ -n "${CHATCOPILOT_CODEX_BOT_HOME:-}" ]; then
            ok "专用代码任务凭据目录已配置（路径已脱敏）"
        else
            warn "专用代码任务凭据未配置；真实代码任务将 fail-closed"
        fi
    elif [ "$CODE_WORKER_REQUIRED" = 0 ]; then
        dim "代码任务工具不适用（BotSpec 未启用 dev.code_tasks）"
    fi
    # ACP server 入口（取代 cursor-agent CLI）
    _venv_py="${CHATCOPILOT_ACP_PY:-$MT_HOME/.venv/bin/python}"
    if [ -x "$_venv_py" ]; then
        if PYTHONPATH="$MT_HOME/src${PYTHONPATH:+:$PYTHONPATH}" "$_venv_py" -c "from chatcopilot.run import main; from chatcopilot.middleware.acp.server import main as acp_main" 2>/dev/null; then
            ok "ACP server: $_venv_py -m chatcopilot run --bot <bot.yaml>"
        else
            bad "ACP server 模块导入失败：检查依赖、chatcopilot 包与 BotSpec 是否已同步"
        fi
    else
        bad "venv python 不可执行：$_venv_py"
    fi

    bold "▶ 隔离代码任务 worker"
    _code_worker_unit="chatcopilot-code-worker@${CHATCOPILOT_INSTANCE_ID}.service"
    if [ "$CODE_WORKER_REQUIRED" = 0 ]; then
        dim "$_code_worker_unit not_applicable（BotSpec 未启用 dev.code_tasks）"
    elif [ "$CODE_WORKER_REQUIRED" = 1 ]; then
        if systemctl --user is-active --quiet "$_code_worker_unit" 2>/dev/null; then
            ok "$_code_worker_unit active"
        else
            bad "$_code_worker_unit inactive"
        fi
    else
        bad "无法从 BotSpec 解析 dev.code_tasks；worker 状态未知"
    fi
    if [ "$CODE_WORKER_REQUIRED" = 1 ] && [ -x "$_venv_py" ]; then
        CHATCOPILOT_STATUS_WS="$WS_ROOT" "$_venv_py" - <<'PY'
import json
import os
import time
from pathlib import Path

root = Path(os.environ["CHATCOPILOT_STATUS_WS"])
jobs = []
for path in root.glob("**/jobs/job_*/status.json"):
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        request = json.loads((path.parent / "request.json").read_text(encoding="utf-8"))
    except Exception:
        continue
    if request.get("tool_name") != "start_code_task":
        continue
    jobs.append((float(status.get("updated_at") or 0), path.parent.name, status))
if not jobs:
    print("      queue=0 latest=none")
else:
    jobs.sort(reverse=True)
    active = {
        "queued", "preparing", "running", "validating", "delivering",
        "cancel_requested",
    }
    queue = sum(str(item[2].get("status") or "") in active for item in jobs)
    _, task_id, status = jobs[0]
    heartbeat = status.get("heartbeat_at")
    age = max(0, int(time.time() - float(heartbeat))) if heartbeat else -1
    resource = status.get("resource") if isinstance(status.get("resource"), dict) else {}
    print(
        "      queue={queue} latest={task} state={state} stage={stage} "
        "heartbeat_age_s={age} rss_mib={rss} disk_mib={disk}".format(
            queue=queue,
            task=task_id,
            state=status.get("status") or "unknown",
            stage=status.get("stage") or "unknown",
            age=age,
            rss=int(resource.get("rss_bytes") or 0) // (1024 * 1024),
            disk=int(resource.get("disk_bytes") or 0) // (1024 * 1024),
        )
    )
PY
    fi

    bold "▶ 常用命令"
    cat <<'EOF'
  # 推荐：所有部署/起停/日志/诊断都走运维控制台 http://localhost:8910
  # 更新实例：cd ~/ChatCopilot && bash deploy/wsl/update_instance.sh --instance <id>
  # 控制台起停（等价 UI 按钮）：bash console/scripts/ctl.sh start|stop|restart <id>
  bash status.sh -w            # 每 5s 刷新本检查（手动排查用）
EOF
    echo
}

if [ "$WATCH" = 1 ]; then
    while true; do
        clear
        if [ "$RUNTIME_KIND" = "gateway" ]; then
            print_gateway_status
        else
            print_status
        fi
        printf "\033[2m(每 %ss 刷新；Ctrl+C 退出)\033[0m\n" "$INTERVAL"
        sleep "$INTERVAL"
    done
else
    if [ "$RUNTIME_KIND" = "gateway" ]; then
        print_gateway_status
    else
        print_status
    fi
fi
