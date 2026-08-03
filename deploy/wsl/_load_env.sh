#!/usr/bin/env bash
# _load_env.sh — WslDeploy 公共环境加载 lib（不可单独执行，必须 source）
#
# 目的：消除 _apply_config.sh / start.sh / status.sh 等脚本各自
# 重复写的「PowerShell 短命调用 → 非交互非登录 shell → ~/.bashrc 顶部 case 直接
# return → export 全跳过」的 env 兜底加载逻辑（重复 5 处，维护时易遗漏）。
#
# 用法（被 source）：
#   # shellcheck source=./_load_env.sh
#   source "$(dirname "$0")/_load_env.sh"
#
#   # 1) 加载机器人主配置（FEISHU_APP_ID/SECRET、CHATCOPILOT_*、WORKSPACE_ROOT）
#   ccp_load_env "FEISHU_APP_ID|FEISHU_APP_SECRET|TAVILY_API_KEY|CHATCOPILOT_|WORKSPACE_ROOT"
#
#   # 2) 仅加载 dump 输出根配置
#   ccp_load_env "CHATCOPILOT_DUMP_ROOT"
#
#   # 3) 把 ~/.npm-global/bin / ~/.local/bin 幂等 prepend 到 PATH
#   ccp_prepend_user_bins
#
# 设计：
# - 只从 ~/.bashrc + ~/.chatcopilot.env（推荐 chmod 600）抽 export 行；
#   抽到独立 tmp 文件 source，避免 ~/.bashrc 里的副作用（alias / function / case）执行。
# - ~/.chatcopilot.env 是机器人运行时的唯一权威配置源，最后加载，覆盖 shell
#   里残留的旧 FEISHU_APP_ID / FEISHU_APP_SECRET，避免 cc-connect 和 Python notifier
#   使用不同飞书应用。
# - 任何步骤异常都 silently 跳过，让调用方继续——env 缺失仍能 fail-loud 在业务层报错。
# - 函数名一律 ccp_ 前缀，避免污染调用脚本命名空间。
# - 反复 source 安全（无副作用 & 幂等）。

CCP_PROJECT_NAME="ChatCopilot"
CCP_PROJECT_SLUG="chatcopilot"
CCP_ENV_PREFIX="CHATCOPILOT"

_ccp_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
_ccp_detected_home="$(cd "$_ccp_script_dir/../.." >/dev/null 2>&1 && pwd)"
_ccp_detected_name="$(basename "$_ccp_detected_home" 2>/dev/null || echo "$CCP_PROJECT_NAME")"

if [ -z "${CHATCOPILOT_INSTANCE_ID:-}" ]; then
    case "$_ccp_detected_name" in
        "$CCP_PROJECT_NAME"-*) CHATCOPILOT_INSTANCE_ID="${_ccp_detected_name#${CCP_PROJECT_NAME}-}" ;;
        *) CHATCOPILOT_INSTANCE_ID="" ;;
    esac
fi
export CHATCOPILOT_INSTANCE_ID

if [ -n "${CHATCOPILOT_HOME:-}" ]; then
    CCP_HOME_DEFAULT="$CHATCOPILOT_HOME"
elif [ -d "$_ccp_detected_home/deploy/wsl" ]; then
    CCP_HOME_DEFAULT="$_ccp_detected_home"
else
    CCP_HOME_DEFAULT="$HOME/$CCP_PROJECT_NAME"
fi

if [ -n "${CHATCOPILOT_INSTANCE_ID:-}" ]; then
    CCP_ENV_FILE_DEFAULT="$HOME/.chatcopilot-${CHATCOPILOT_INSTANCE_ID}.env"
    CCP_WORKSPACE_ROOT_DEFAULT="$HOME/${CCP_PROJECT_SLUG}-workspaces/${CHATCOPILOT_INSTANCE_ID}"
    CCP_LOG_DIR_DEFAULT="$HOME/${CCP_PROJECT_SLUG}-logs/${CHATCOPILOT_INSTANCE_ID}"
    CCP_CC_HOME_DEFAULT="$HOME/.${CCP_PROJECT_SLUG}-runtime/${CHATCOPILOT_INSTANCE_ID}"
    CCP_CC_PROJECT_NAME_DEFAULT="${CCP_PROJECT_SLUG}-${CHATCOPILOT_INSTANCE_ID}"
else
    CCP_ENV_FILE_DEFAULT="$HOME/.chatcopilot.env"
    CCP_WORKSPACE_ROOT_DEFAULT="$HOME/${CCP_PROJECT_SLUG}-workspaces"
    CCP_LOG_DIR_DEFAULT="$HOME/${CCP_PROJECT_SLUG}-logs"
    CCP_CC_HOME_DEFAULT="$HOME"
    CCP_CC_PROJECT_NAME_DEFAULT="$CCP_PROJECT_SLUG"
fi

CCP_ENV_FILE="${CHATCOPILOT_ENV_FILE:-$CCP_ENV_FILE_DEFAULT}"
CCP_CC_HOME="${CHATCOPILOT_CC_HOME:-$CCP_CC_HOME_DEFAULT}"
CCP_CC_CONFIG_DIR="${CHATCOPILOT_CC_CONNECT_CONFIG_DIR:-$CCP_CC_HOME/.cc-connect}"
CCP_CC_PROJECT_NAME="${CHATCOPILOT_CC_PROJECT_NAME:-$CCP_CC_PROJECT_NAME_DEFAULT}"

ccp_refresh_derived_defaults() {
    CCP_ENV_FILE="${CHATCOPILOT_ENV_FILE:-$CCP_ENV_FILE_DEFAULT}"
    CCP_CC_HOME="${CHATCOPILOT_CC_HOME:-$CCP_CC_HOME_DEFAULT}"
    CCP_CC_CONFIG_DIR="${CHATCOPILOT_CC_CONNECT_CONFIG_DIR:-$CCP_CC_HOME/.cc-connect}"
    CCP_CC_PROJECT_NAME="${CHATCOPILOT_CC_PROJECT_NAME:-$CCP_CC_PROJECT_NAME_DEFAULT}"
}

# ---------------------------------------------------------------------------
# ccp_bot_chat_env_prefix <bot.yaml>
#   读取 BotSpec 的 llm.chat.env_prefix；兼容旧 llm.env_prefix，并与 loader
#   的 CHATCOPILOT_CHAT 默认值保持一致。仅解析这个简单标量，不执行 YAML。
# ---------------------------------------------------------------------------
ccp_bot_chat_env_prefix() {
    local _bot="${1:-}"
    [ -r "$_bot" ] || return 0
    awk '
        {
            line = $0
            sub(/[[:space:]]+#.*/, "", line)
            if (line ~ /^[[:space:]]*$/) next
            match(line, /^[[:space:]]*/)
            indent = RLENGTH
            content = substr(line, indent + 1)
            if (indent == 0) {
                in_llm = (content ~ /^llm[[:space:]]*:[[:space:]]*$/)
                llm_indent = indent
                in_chat = 0
                next
            }
            if (!in_llm) next
            if (in_chat && indent <= chat_indent) in_chat = 0
            if (!in_chat && indent == llm_indent + 2 \
                    && content ~ /^chat[[:space:]]*:[[:space:]]*$/) {
                in_chat = 1
                chat_indent = indent
                next
            }
            if (content ~ /^env_prefix[[:space:]]*:/) {
                sub(/^env_prefix[[:space:]]*:[[:space:]]*/, "", content)
                gsub(/^[[:space:]"\047]+|[[:space:]"\047]+$/, "", content)
                if (in_chat && indent > chat_indent) {
                    print content
                    found = 1
                    exit
                }
                if (!in_chat && indent == llm_indent + 2) legacy = content
            }
        }
        END {
            if (!found) print (legacy != "" ? legacy : "CHATCOPILOT_CHAT")
        }
    ' "$_bot"
}

# ---------------------------------------------------------------------------
# ccp_apply_bot_deploy_config
#   从 BotSpec 的 deploy 段读取 WSL 部署路径。它只处理简单的 "key: value"
#   标量配置，避免部署脚本依赖 PyYAML。
# ---------------------------------------------------------------------------
ccp_apply_bot_deploy_config() {
    local _bot="${CHATCOPILOT_BOT_SPEC:-}"
    if [ -z "$_bot" ]; then
        if [ -n "${CHATCOPILOT_INSTANCE_ID:-}" ] \
            && [ -f "$CCP_HOME_DEFAULT/bots/$CHATCOPILOT_INSTANCE_ID/bot.yaml" ]; then
            _bot="$CCP_HOME_DEFAULT/bots/$CHATCOPILOT_INSTANCE_ID/bot.yaml"
        elif [ -f "$CCP_HOME_DEFAULT/bots/lingye-copilot-qq/bot.yaml" ]; then
            _bot="$CCP_HOME_DEFAULT/bots/lingye-copilot-qq/bot.yaml"
        else
            _bot="$(find "$CCP_HOME_DEFAULT/bots" -mindepth 2 -maxdepth 2 -name bot.yaml 2>/dev/null | head -n 1 || true)"
        fi
    fi
    [ -n "$_bot" ] && [ -f "$_bot" ] || return 0

    local _tmp
    _tmp="$(mktemp 2>/dev/null)" || return 0
    python3 - "$_bot" <<'PY' > "$_tmp" 2>/dev/null || true
import os
import shlex
import sys
from pathlib import Path

bot = Path(sys.argv[1]).expanduser().resolve()
deploy = {}
top = {}
section = ""
for raw in bot.read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw.split("#", 1)[0].rstrip()
    if not line.strip():
        continue
    if not raw[:1].isspace() and ":" in line:
        key, value = line.split(":", 1)
        section = key.strip()
        value = value.strip().strip("\"'")
        if value:
            top[section] = value
        continue
    if section == "deploy" and raw[:1].isspace() and ":" in line:
        key, value = line.split(":", 1)
        deploy[key.strip()] = value.strip().strip("\"'")

home = Path.home()
def expand(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value == "~":
        return str(home)
    if value.startswith("~/"):
        return str(home / value[2:])
    return value

exports = {
    "CHATCOPILOT_BOT_SPEC": str(bot),
}
mapping = {
    "instance_id": "CHATCOPILOT_INSTANCE_ID",
    "wsl_home": "CHATCOPILOT_HOME",
    "workspace_root": "CHATCOPILOT_WORKSPACE_ROOT",
    "log_dir": "CHATCOPILOT_LOG_DIR",
    "env_file": "CHATCOPILOT_ENV_FILE",
    "cc_connect_config_dir": "CHATCOPILOT_CC_CONNECT_CONFIG_DIR",
    "project_name": "CHATCOPILOT_CC_PROJECT_NAME",
}
for key, env_name in mapping.items():
    value = deploy.get(key, "")
    if not value:
        continue
    exports[env_name] = expand(value) if key.endswith("_home") or key.endswith("_root") or key.endswith("_dir") or key == "env_file" else value

if exports.get("CHATCOPILOT_WORKSPACE_ROOT"):
    exports["WORKSPACE_ROOT"] = exports["CHATCOPILOT_WORKSPACE_ROOT"]
config_dir = exports.get("CHATCOPILOT_CC_CONNECT_CONFIG_DIR", "")
suffix = "/.cc-connect"
if config_dir.endswith(suffix):
    exports["CHATCOPILOT_CC_HOME"] = config_dir[: -len(suffix)] or str(home)
if top.get("display_name"):
    exports["CHATCOPILOT_DISPLAY_NAME"] = top["display_name"]

for key, value in exports.items():
    print(f"export {key}={shlex.quote(str(value))}")
PY
    if [ -s "$_tmp" ]; then
        set -a
        # shellcheck disable=SC1090
        source "$_tmp"
        set +a
    fi
    rm -f "$_tmp"
    ccp_refresh_derived_defaults
}

# ---------------------------------------------------------------------------
# ccp_load_env <egrep_pattern>
#   把匹配 ^[[:space:]]*export[[:space:]]+(<pattern>) 的行从已知 env 文件里
#   抽出，临时 source 到当前 shell。pattern 是 grep -E 的扩展正则，括号 / 管道
#   要按 ERE 写。
# ---------------------------------------------------------------------------
ccp_load_env() {
    local pattern="${1:-}"
    if [ -z "$pattern" ]; then
        return 0
    fi
    local _envfile _tmp
    for _envfile in "$HOME/.bashrc" "$CCP_ENV_FILE"; do
        [ -r "$_envfile" ] || continue
        _tmp="$(mktemp 2>/dev/null)" || continue
        grep -E "^[[:space:]]*export[[:space:]]+(${pattern})" "$_envfile" > "$_tmp" 2>/dev/null || true
        if [ -s "$_tmp" ]; then
            set -a
            # shellcheck disable=SC1090
            source "$_tmp"
            set +a
        fi
        rm -f "$_tmp"
    done
}

# ---------------------------------------------------------------------------
# ccp_prepend_user_bins
#   把 ~/.npm-global/bin 与 ~/.local/bin 幂等 prepend 到 PATH（cc-connect 装在
#   .npm-global/bin/，非交互 shell 默认拿不到）。
# ---------------------------------------------------------------------------
ccp_prepend_user_bins() {
    local _user_bin
    for _user_bin in "$HOME/.npm-global/bin" "$HOME/.local/bin"; do
        case ":$PATH:" in
            *":$_user_bin:"*) ;;
            *) [ -d "$_user_bin" ] && PATH="$_user_bin:$PATH" ;;
        esac
    done
    export PATH
}
