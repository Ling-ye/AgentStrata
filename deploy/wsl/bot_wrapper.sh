#!/usr/bin/env bash
# bot_wrapper.sh — cc-connect 启动 AgentStrata ACP runtime 时实际跑的包装脚本
#
# 会话身份由实例私有、哈希命名的 JSON 文件传递。wrapper 不 source 该文件；最终由
# ``bot exec-session-runtime`` 以 O_NOFOLLOW、owner/mode/link 检查读取白名单字段后
# exec ACP runtime，保留 cc-connect 的 stdin/stdout ACP 通道。
set -euo pipefail
umask 077

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

if [ ! -x "$PY" ]; then
    echo "[bot_wrapper] python interpreter not executable: $PY" >&2
    exit 127
fi

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
    CONFIG_APP_ID="$(sed -n 's/^[[:space:]]*app_id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CC_CONFIG" | head -n 1 || true)"
    if [ -n "$CONFIG_APP_ID" ] && [ "$CONFIG_APP_ID" != "$FEISHU_APP_ID" ]; then
        echo "[bot_wrapper] ERROR: Feishu app mismatch between runtime env and cc-connect config" >&2
        exit 78
    fi
fi

cd "$MT_HOME" || {
    echo "[bot_wrapper] cannot cd to $MT_HOME" >&2
    exit 1
}
export PYTHONPATH="$MT_HOME/src${PYTHONPATH:+:$PYTHONPATH}"

SESS_KEY="${CC_SESSION_KEY:-${CC_HOOK_SESSION_KEY:-}}"
if [ -z "$SESS_KEY" ]; then
    echo "[bot_wrapper] session key is empty; runtime will enforce platform identity boundaries" >&2
    exec "$PY" -m chatcopilot run --bot "$BOT_SPEC" "$@"
fi

SESSION_ENV_DIR="${CHATCOPILOT_SESSION_ENV_DIR:-$CC_HOME/session-env}"
case "$SESSION_ENV_DIR" in
    /*) ;;
    *)
        echo "[bot_wrapper] session env directory must be absolute" >&2
        exit 78
        ;;
esac
if ! command -v sha256sum >/dev/null 2>&1; then
    echo "[bot_wrapper] sha256sum is unavailable" >&2
    exit 69
fi
SESS_HASH="$(printf '%s' "$SESS_KEY" | sha256sum | awk '{print $1}')"
SESS_ENV="$SESSION_ENV_DIR/cc-sess-${SESS_HASH}.env"

# A message.received hook normally creates this first. The fallback only creates
# conversation identity when no filesystem entry exists at all. An unsafe or
# preoccupied entry is left untouched so the strict reader below fails closed.
if [ ! -e "$SESS_ENV" ] && [ ! -L "$SESS_ENV" ]; then
    if ! "$PY" -m chatcopilot bot render-session-env \
        --bot "$BOT_SPEC" \
        --session-key "$SESS_KEY" \
        --session-env-dir "$SESSION_ENV_DIR" >/dev/null; then
        echo "[bot_wrapper] session identity bootstrap failed" >&2
        exit 78
    fi
fi

exec "$PY" -m chatcopilot bot exec-session-runtime \
    --bot "$BOT_SPEC" \
    --session-env-dir "$SESSION_ENV_DIR" \
    --session-key "$SESS_KEY" \
    -- "$@"
