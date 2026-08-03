#!/usr/bin/env bash
# bot_wrapper.sh — cc-connect 启动 AgentStrata ACP runtime 时实际跑的包装脚本
#
# 用途：把 session.started hook 写入的 sess env 文件 source 进当前进程，让
# CHATCOPILOT_USER_ID / CHAT_ID / CHAT_KIND 这几个变量沿
# wrapper → python -m chatcopilot run 一路继承下去，从而让
# middleware.runtime.workspace.resolve_workspace() 能算出 per-user 工作目录。
#
# 调用链：
#   cc-connect ──spawn (type=acp)──▶ bot_wrapper.sh ──source ENV──▶ exec python -m chatcopilot run
#                                      │
#         hook ─writes─▶  /tmp/cc-sess-${CC_SESSION_KEY}.env
#
# 注意：
# - 必须配合 `session.started` hook (async=false) 一起使用，否则 sess env 文件
#   还没写完 wrapper 就开跑，会读到空值，全部 fallback 到 default 目录。
# - 出错时 fail-open：env 缺失就直接 exec ACP server，让进程能起来；这种情况下
#   工作区会退化为 default 共享目录，但功能不挂——agent 自我介绍时会按 persona.py
#   里的 fallback 模板告诉用户"本次会话未绑定身份"。
# - 由 ``CHATCOPILOT_HOME`` 找到仓库根（cc-connect 配置注入），用 .venv/bin/python
#   作为解释器；解释器路径可被 ``CHATCOPILOT_ACP_PY`` 覆盖（调试用）。

set -uo pipefail

# ----------------------------------------------------------------------------
# 1. 加载实例 env —— 全局机密配置（不 commit 进 git）
# ----------------------------------------------------------------------------
# LLM 变量名由当前 BotSpec 的 ``llm.chat.env_prefix`` 决定：
#   export <env_prefix>_API_KEY="sk-xxxxxx"
#   export <env_prefix>_BASE_URL="https://llm.example.com"
#   export <env_prefix>_MODEL="provider/model"
#   export FEISHU_APP_ID="cli_xxx"      # 已存在；_apply_config.sh / start.sh 也读它
#   export FEISHU_APP_SECRET="xxxxxx"   # 同上
#
# 安全：env 文件应当 chmod 600，避免同机其他用户读到。
# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"

ENVFILE="$CCP_ENV_FILE"
if [ -r "$ENVFILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENVFILE"
    set +a
    echo "[bot_wrapper] loaded $ENVFILE" >&2
else
    echo "[bot_wrapper] $ENVFILE not found; relying on cc-connect-injected env for LLM credentials" >&2
fi

CC_HOME="${CHATCOPILOT_CC_HOME:-$CCP_CC_HOME_DEFAULT}"
CC_CONFIG_DIR="${CHATCOPILOT_CC_CONNECT_CONFIG_DIR:-$CC_HOME/.cc-connect}"
CC_CONFIG="$CC_CONFIG_DIR/config.toml"
MT_HOME="${CHATCOPILOT_HOME:-$CCP_HOME_DEFAULT}"
PY="${CHATCOPILOT_ACP_PY:-$MT_HOME/.venv/bin/python}"
BOT_SPEC="${CHATCOPILOT_BOT_SPEC:-$MT_HOME/bots/${CHATCOPILOT_INSTANCE_ID:-lingye-copilot-qq}/bot.yaml}"
CHAT_LLM_ENV_PREFIX="$(ccp_bot_chat_env_prefix "$BOT_SPEC")"
if [[ "$CHAT_LLM_ENV_PREFIX" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    CHAT_LLM_API_KEY_VAR="${CHAT_LLM_ENV_PREFIX}_API_KEY"
    CHAT_LLM_BASE_URL_VAR="${CHAT_LLM_ENV_PREFIX}_BASE_URL"
    CHAT_LLM_MODEL_VAR="${CHAT_LLM_ENV_PREFIX}_MODEL"
    CHAT_LLM_API_KEY="${!CHAT_LLM_API_KEY_VAR:-}"
    CHAT_LLM_BASE_URL="${!CHAT_LLM_BASE_URL_VAR:-}"
    CHAT_LLM_MODEL="${!CHAT_LLM_MODEL_VAR:-}"
    if [ -n "$CHAT_LLM_API_KEY" ]; then
        CHAT_LLM_CREDENTIAL_STATUS="present"
    else
        CHAT_LLM_CREDENTIAL_STATUS="missing"
    fi
    echo "[bot_wrapper] llm.chat env_prefix=$CHAT_LLM_ENV_PREFIX (API_KEY=$CHAT_LLM_CREDENTIAL_STATUS, BASE_URL=$([ -n "$CHAT_LLM_BASE_URL" ] && echo present || echo missing), MODEL=${CHAT_LLM_MODEL:-missing})" >&2
else
    echo "[bot_wrapper] warning: cannot resolve llm.chat.env_prefix from $BOT_SPEC" >&2
fi
if [ -n "${FEISHU_APP_ID:-}" ] && [ -r "$CC_CONFIG" ]; then
    CONFIG_APP_ID="$(sed -n 's/^[[:space:]]*app_id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CC_CONFIG" | head -n 1)"
    if [ -n "$CONFIG_APP_ID" ] && [ "$CONFIG_APP_ID" != "$FEISHU_APP_ID" ]; then
        echo "[bot_wrapper] ERROR: Feishu app mismatch: $ENVFILE has $FEISHU_APP_ID, $CC_CONFIG has $CONFIG_APP_ID" >&2
        echo "[bot_wrapper] Run: cd $CCP_HOME_DEFAULT/deploy/wsl && bash start.sh --apply-config" >&2
        exit 78
    fi
fi

# ----------------------------------------------------------------------------
# 2. 加载 session.started hook 写入的 per-session env（user_id / chat_id / chat_kind）
# ----------------------------------------------------------------------------
SESS_KEY="${CC_SESSION_KEY:-${CC_HOOK_SESSION_KEY:-}}"
if [ -n "$SESS_KEY" ]; then
    SESS_ENV="/tmp/cc-sess-${SESS_KEY}.env"
    if [ -r "$SESS_ENV" ]; then
        set -a
        # shellcheck disable=SC1090
        source "$SESS_ENV"
        set +a
        echo "[bot_wrapper] loaded $SESS_ENV (user=${CHATCOPILOT_USER_ID:-?} chat=${CHATCOPILOT_CHAT_KIND:-?}/${CHATCOPILOT_CHAT_ID:-?})" >&2
    else
        echo "[bot_wrapper] sess env not found: $SESS_ENV" >&2
    fi
else
    echo "[bot_wrapper] CC_SESSION_KEY empty" >&2
fi

# 兜底：如果 sess env 文件没写 / 写空了，委托 PlatformAdapter 解析 session key。
# wrapper 只做跨进程 env 搬运，不内置任何平台 session key 形态。
if [ -z "${CHATCOPILOT_USER_ID:-}" ] && [ -n "$SESS_KEY" ] && [ -x "$PY" ] && [ -f "$BOT_SPEC" ]; then
    _sess_tmp="$(mktemp 2>/dev/null || true)"
    if [ -n "$_sess_tmp" ] && "$PY" -m chatcopilot bot render-session-env \
        --bot "$BOT_SPEC" \
        --session-key "$SESS_KEY" \
        --user-id "${CC_HOOK_USER_ID:-}" \
        --chat-id "${CC_HOOK_CHAT_ID:-}" \
        --chat-kind "${CC_HOOK_CHAT_KIND:-${CC_HOOK_CHAT_TYPE:-${CC_HOOK_MESSAGE_TYPE:-${CC_HOOK_EVENT_TYPE:-}}}}" \
        --user-name "${CC_HOOK_USER_NAME:-}" > "$_sess_tmp" 2>/dev/null; then
        set -a
        # shellcheck disable=SC1090
        source "$_sess_tmp"
        set +a
        echo "[bot_wrapper] fell back to render-session-env (user=${CHATCOPILOT_USER_ID:-?} chat=${CHATCOPILOT_CHAT_KIND:-?}/${CHATCOPILOT_CHAT_ID:-?})" >&2
    fi
    [ -z "${_sess_tmp:-}" ] || rm -f "$_sess_tmp"
fi

# 最后一层兜底：只使用 cc-connect 明确给出的 hook 字段；不解析 session key。
if [ -z "${CHATCOPILOT_USER_ID:-}" ] && [ -n "${CC_HOOK_USER_ID:-}" ]; then
    export CHATCOPILOT_USER_ID="$CC_HOOK_USER_ID"
    export CHATCOPILOT_CHAT_ID="${CC_HOOK_CHAT_ID:-${CHATCOPILOT_CHAT_ID:-}}"
    export CHATCOPILOT_CHAT_KIND="${CC_HOOK_CHAT_KIND:-${CC_HOOK_CHAT_TYPE:-${CC_HOOK_MESSAGE_TYPE:-${CC_HOOK_EVENT_TYPE:-${CHATCOPILOT_CHAT_KIND:-p2p}}}}}"
    export CHATCOPILOT_USER_NAME="${CC_HOOK_USER_NAME:-${CHATCOPILOT_USER_NAME:-}}"
    echo "[bot_wrapper] fell back to CC_HOOK_* identity (user=$CHATCOPILOT_USER_ID chat=$CHATCOPILOT_CHAT_KIND/$CHATCOPILOT_CHAT_ID)" >&2
fi

if [ ! -x "$PY" ]; then
    echo "[bot_wrapper] python interpreter not executable: $PY" >&2
    exit 127
fi

cd "$MT_HOME" || {
    echo "[bot_wrapper] cannot cd to $MT_HOME" >&2
    exit 1
}

export PYTHONPATH="$MT_HOME/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m chatcopilot run --bot "$BOT_SPEC" "$@"
