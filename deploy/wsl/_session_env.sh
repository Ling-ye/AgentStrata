#!/usr/bin/env bash
# _session_env.sh — cc-connect message transport identity hook
#
# ``message.received`` 把 conversation identity 与独立 transport attestation 追加到实例
# 私有 ``session-env`` 目录的有界加锁队列。文件名只包含 session key 的 SHA-256，state 是 JSON；
# 该文件绝不由 shell source。
set -euo pipefail
umask 077

# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"
ccp_prepend_user_bins
ccp_load_env "CHATCOPILOT_|WORKSPACE_ROOT"
ccp_apply_bot_deploy_config
export PYTHONPATH="$CCP_HOME_DEFAULT/src${PYTHONPATH:+:$PYTHONPATH}"

if [ "${CC_HOOK_EVENT:-}" != "message.received" ]; then
    echo "[_session_env] unsupported or missing hook event" >&2
    exit 64
fi

SESS_KEY="${CC_HOOK_SESSION_KEY:-${CC_SESSION_KEY:-}}"
if [ -z "$SESS_KEY" ]; then
    echo "[_session_env] session key is empty" >&2
    exit 64
fi

PY="${CHATCOPILOT_ACP_PY:-$CCP_HOME_DEFAULT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "[_session_env] Python interpreter is unavailable" >&2
    exit 69
fi

BOT_SPEC="${CHATCOPILOT_BOT_SPEC:-}"
if [ -z "$BOT_SPEC" ] || [ ! -f "$BOT_SPEC" ]; then
    echo "[_session_env] BotSpec is unavailable" >&2
    exit 78
fi

CC_HOME="${CHATCOPILOT_CC_HOME:-$CCP_CC_HOME_DEFAULT}"
SESSION_ENV_DIR="${CHATCOPILOT_SESSION_ENV_DIR:-$CC_HOME/session-env}"
case "$SESSION_ENV_DIR" in
    /*) ;;
    *)
        echo "[_session_env] session env directory must be absolute" >&2
        exit 78
        ;;
esac

if ! "$PY" -m chatcopilot bot render-session-env \
    --bot "$BOT_SPEC" \
    --session-key "$SESS_KEY" \
    --session-env-dir "$SESSION_ENV_DIR"; then
    echo "[_session_env] private session attestation write failed" >&2
    exit 78
fi

echo "[_session_env] refreshed private message attestation" >&2
exit 0
