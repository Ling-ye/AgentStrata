#!/usr/bin/env bash
# _apply_config.sh — 仅为 legacy edge 渲染 cc-connect 配置。
#
# 平台特定知识（需要哪些凭据、cc-connect [[projects.platforms]] 片段、额外配置
# 文件如飞书 .lark-cli/config.json）已下沉到 src/chatcopilot/platforms/<type>/adapter.py。
# 本脚本只负责：加载 env、确保目录、把已解析的部署路径导出给 CLI，再调用
#   python -m chatcopilot bot doctor          （按 adapter.required_secrets 校验凭据）
#   python -m chatcopilot bot render-cc-config （渲染完整 config.toml + 平台额外文件）
# 新增平台无需改动本脚本——只要 platforms/<name>/adapter.py 暴露 ADAPTER。
#
# 期望的通用环境变量：
#   CHATCOPILOT_HOME   (默认当前部署目录，或 $HOME/ChatCopilot)
#   WORKSPACE_ROOT     (默认 $HOME/chatcopilot-workspaces[/<instance>])
#   CHATCOPILOT_INSTANCE_ID
#   CHATCOPILOT_CC_HOME / CHATCOPILOT_CC_CONNECT_CONFIG_DIR
set -uo pipefail

# 公共 env 加载（消除以前各脚本重复的 grep + source 兜底逻辑；详见 _load_env.sh）
# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"
ccp_apply_bot_deploy_config
# ~/.chatcopilot-<instance>.env 是运行时权威配置源；渲染 cc-connect 前总是重载，
# 避免当前 shell 中的旧凭证把 config.toml 渲染成另一个机器人。
# 各平台凭据（FEISHU_* / QQ_* / 后续平台）一并加载，由 adapter 决定取用哪些。
ccp_load_env "FEISHU_APP_ID|FEISHU_APP_SECRET|TAVILY_API_KEY|QQ_|CHATCOPILOT_|WORKSPACE_ROOT"
ccp_apply_bot_deploy_config

MT_HOME="${CHATCOPILOT_HOME:-$CCP_HOME_DEFAULT}"
WS_ROOT="${WORKSPACE_ROOT:-${CHATCOPILOT_WORKSPACE_ROOT:-$CCP_WORKSPACE_ROOT_DEFAULT}}"
WS_DEFAULT="$WS_ROOT/default"
BOT_SPEC="${CHATCOPILOT_BOT_SPEC:-$MT_HOME/bots/${CHATCOPILOT_INSTANCE_ID:-lingye-copilot-qq}/bot.yaml}"
VENV_PY="$MT_HOME/.venv/bin/python"
LOG_DIR="${CHATCOPILOT_LOG_DIR:-$CCP_LOG_DIR_DEFAULT}"
CC_HOME="${CHATCOPILOT_CC_HOME:-$CCP_CC_HOME_DEFAULT}"
CC_CONFIG_DIR="${CHATCOPILOT_CC_CONNECT_CONFIG_DIR:-$CC_HOME/.cc-connect}"
CC_PROJECT_NAME="${CHATCOPILOT_CC_PROJECT_NAME:-$CCP_CC_PROJECT_NAME}"
CC_DISPLAY_NAME="${CHATCOPILOT_DISPLAY_NAME:-${CHATCOPILOT_INSTANCE_ID:-AgentStrata}}"

if [ ! -f "$BOT_SPEC" ]; then
    echo "[ERR] 找不到 BotSpec: $BOT_SPEC" >&2
    exit 1
fi

# ---------- 把已解析的部署路径/标识导出给 CLI（保证与 bash 计算结果一致） ----------
export CHATCOPILOT_HOME="$MT_HOME"
export CHATCOPILOT_WORKSPACE_ROOT="$WS_ROOT"
export WORKSPACE_ROOT="$WS_ROOT"
export CHATCOPILOT_BOT_SPEC="$BOT_SPEC"
export CHATCOPILOT_LOG_DIR="$LOG_DIR"
export CHATCOPILOT_CC_HOME="$CC_HOME"
export CHATCOPILOT_CC_CONNECT_CONFIG_DIR="$CC_CONFIG_DIR"
export CHATCOPILOT_CC_PROJECT_NAME="$CC_PROJECT_NAME"
export CHATCOPILOT_DISPLAY_NAME="$CC_DISPLAY_NAME"
export CHATCOPILOT_ENV_FILE="${CHATCOPILOT_ENV_FILE:-$CCP_ENV_FILE}"

# ---------- 选择 python（优先 venv，回退系统 python3） ----------
if [ -x "$VENV_PY" ]; then
    PY="$VENV_PY"
else
    PY="$(command -v python3 || command -v python)"
fi
if [ -z "${PY:-}" ]; then
    echo "[ERR] 未找到可用的 python 解释器" >&2
    exit 1
fi
export PYTHONPATH="$MT_HOME/src${PYTHONPATH:+:$PYTHONPATH}"

if ccp_bot_uses_gateway "$BOT_SPEC"; then
    echo "[OK] Gateway-backed instance does not render cc-connect/session-env configuration."
    echo "  instance: ${CHATCOPILOT_INSTANCE_ID:-default}"
    echo "  bot spec: $BOT_SPEC"
    echo "  runtime:  python -m chatcopilot run --bot $BOT_SPEC"
    exit 0
fi

mkdir -p "$CC_CONFIG_DIR" "$WS_DEFAULT/downloads" "$WS_DEFAULT/results" "$WS_DEFAULT/uploads" "$WS_DEFAULT/.cc-connect/attachments" "$LOG_DIR"

# ---------- 1. 校验平台凭据（adapter.required_secrets 驱动，平台无关） ----------
if ! "$PY" -m chatcopilot bot doctor --bot "$BOT_SPEC"; then
    echo "[ERR] 平台凭据校验失败；请检查实例 env 文件：$CCP_ENV_FILE" >&2
    exit 1
fi

# ---------- 2. 渲染完整 cc-connect 配置（骨架 + 平台片段 + 额外文件，由 CLI 统一生成） ----------
if ! "$PY" -m chatcopilot bot render-cc-config --bot "$BOT_SPEC" --out "$CC_CONFIG_DIR/config.toml"; then
    echo "[ERR] cc-connect 配置渲染失败" >&2
    exit 1
fi

# 3. 确保 cc-connect 调用的脚本都可执行（git clone 后第一次部署需要）
for _script in bot_wrapper.sh _session_env.sh; do
    _path="$MT_HOME/deploy/wsl/$_script"
    if [ -f "$_path" ]; then
        chmod +x "$_path"
        echo "[OK] chmod +x $_path"
    fi
done

PLATFORM_TYPE="$(BOT_SPEC="$BOT_SPEC" "$PY" - <<'PY'
import os
from chatcopilot.botspec.loader import load_botspec
try:
    print(load_botspec(os.environ["BOT_SPEC"]).platform.type)
except Exception:
    print("")
PY
)"

echo
echo "[OK] 配置渲染完成。"
echo "  instance:   ${CHATCOPILOT_INSTANCE_ID:-default}"
echo "  platform:   ${PLATFORM_TYPE:-?}"
echo "  cc home:    $CC_HOME"
echo "  cc-connect: $CC_CONFIG_DIR/config.toml"
echo "  agent type: acp (chatcopilot run)"
echo "  bot spec:   $BOT_SPEC"
echo "  work_dir:   $WS_DEFAULT"
echo "  venv py:    $VENV_PY"
echo "  env file:   $CCP_ENV_FILE"
echo
echo "  注意：ACP server 内进程直接复用 agent 工具集，不再走 stdio MCP；"
echo "        system prompt 由 BotSpec + platforms/<type>/adapter.py 装配，改 prompt = git commit。"
