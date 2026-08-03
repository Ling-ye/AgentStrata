#!/usr/bin/env bash
# status.sh — cc-connect 健康检查（进程 / WebSocket / 配置 / 日志 / 环境）
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

# 解析当前 BotSpec 的 platform.type，决定后面如何展示凭据 / 健康标志
PLATFORM_TYPE_FOR_STATUS=""
if [ -n "${CHATCOPILOT_BOT_SPEC:-}" ] && [ -f "$CHATCOPILOT_BOT_SPEC" ]; then
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
    echo "[ERR] BotSpec 未声明可识别的 platform.type：$CHATCOPILOT_BOT_SPEC" >&2
    exit 2
fi

# cc-connect 主日志查找顺序：CC_LOG_FILE → $LOG_DIR/cc-connect/<date>.log →
# $LOG_DIR/cc-connect/current.log → /tmp/cc-connect.log（legacy）
Q_LOG_DIR="${CHATCOPILOT_LOG_DIR:-$CCP_LOG_DIR_DEFAULT}"
_resolve_cc_log_for_status() {
    if [ -n "${CC_LOG_FILE:-}" ]; then
        echo "$CC_LOG_FILE"; return
    fi
    local today_log="$Q_LOG_DIR/cc-connect/$(date +%F).log"
    [ -e "$today_log" ] && { echo "$today_log"; return; }
    local current_link="$Q_LOG_DIR/cc-connect/current.log"
    [ -e "$current_link" ] && { echo "$current_link"; return; }
    echo "/tmp/cc-connect.log"
}
CC_LOG="$(_resolve_cc_log_for_status)"
Q_LOG="$Q_LOG_DIR/$(date +%F).log"
ERR_LOG="$Q_LOG_DIR/_hook_errors.log"
CC_HOME="${CHATCOPILOT_CC_HOME:-$CCP_CC_HOME_DEFAULT}"
CC_CONFIG_DIR="${CHATCOPILOT_CC_CONNECT_CONFIG_DIR:-$CC_HOME/.cc-connect}"
CC_CONF="$CC_CONFIG_DIR/config.toml"
CC_CONNECT_BIN="${CHATCOPILOT_CC_CONNECT_BIN:-$HOME/.npm-global/bin/cc-connect}"
CURSOR_MCP="$HOME/.cursor/mcp.json"
MT_HOME="${CHATCOPILOT_HOME:-$CCP_HOME_DEFAULT}"
WS_ROOT="${WORKSPACE_ROOT:-${CHATCOPILOT_WORKSPACE_ROOT:-$CCP_WORKSPACE_ROOT_DEFAULT}}"

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

_filter_instance_pids() {
    # 与 _stop_cc.sh 同款：仅返回 environ HOME=$CC_HOME 的 cc-connect PID；
    # 多实例并行时避免把别的实例计入"本实例进程"。
    local pidfile="$CC_HOME/cc-connect.pid"
    local pids=""
    if [ -r "$pidfile" ]; then
        local _p
        _p="$(tr -d ' \t\r\n' < "$pidfile" 2>/dev/null || true)"
        if [ -n "$_p" ] && kill -0 "$_p" 2>/dev/null; then
            pids="$_p"
        fi
    fi
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
            dim "Fix: npm config set prefix $HOME/.npm-global && npm install -g cc-connect@beta && restart the instance"
        fi

        echo
        printf "  \033[2m最后 5 行：\033[0m\n"
        tail -n 5 "$CC_LOG" 2>/dev/null | sed 's/^/      /'
    else
        warn "日志文件不存在：$CC_LOG（cc-connect 可能从未启动）"
    fi

    bold "▶ 用户提问日志"
    if [ -f "$Q_LOG" ]; then
        local q_count
        q_count=$(wc -l < "$Q_LOG")
        local q_age
        q_age=$(( $(date +%s) - $(stat -c %Y "$Q_LOG") ))
        ok "$Q_LOG （$q_count 行，上次更新 $(human_age $q_age) 前）"
    else
        warn "今日还无用户提问：$Q_LOG"
    fi
    if [ -f "$ERR_LOG" ] && [ -s "$ERR_LOG" ]; then
        warn "hook 错误日志非空：$ERR_LOG"
        dim "查看：tail -n 30 $ERR_LOG"
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
                ok "QQ_ALLOW_FROM = (已设置，标识已脱敏)"
            else
                warn "QQ_ALLOW_FROM 未设置（将渲染为 '*' 不限；生产环境建议限制白名单）"
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
    if [ "${CC_CONNECT_BIN#/}" != "$CC_CONNECT_BIN" ] && [ -x "$CC_CONNECT_BIN" ]; then
        local resolved_cc
        resolved_cc="$(readlink -f "$CC_CONNECT_BIN")"
        case "$resolved_cc" in
            /usr/bin/*|/usr/local/bin/*)
                bad "cc-connect -> $resolved_cc（系统 wrapper 被禁止）"
                ;;
            *)
                ok "cc-connect -> $resolved_cc"
                local cc_version
                cc_version="$("$resolved_cc" --version 2>&1 | head -n 1)"
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
    if systemctl --user is-active --quiet "$_code_worker_unit" 2>/dev/null; then
        ok "$_code_worker_unit active"
    else
        bad "$_code_worker_unit inactive"
    fi
    if [ -x "$_venv_py" ]; then
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
        print_status
        printf "\033[2m(每 %ss 刷新；Ctrl+C 退出)\033[0m\n" "$INTERVAL"
        sleep "$INTERVAL"
    done
else
    print_status
fi
