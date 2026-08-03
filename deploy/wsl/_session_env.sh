#!/usr/bin/env bash
# _session_env.sh — cc-connect session identity hook
#
# 作用：把当前会话的 user_id / chat_id / chat_kind 写到 /tmp/cc-sess-<SESSION_KEY>.env，
# 由 bot_wrapper.sh 在 exec ACP server 之前 source 之，从而让
# middleware.runtime.workspace.resolve_workspace() 能算出 per-user 工作目录。
#
# 调用入口：~/.cc-connect/config.toml 的 [[hooks]] event=session.started / message.received，
#          async=false（必须等 hook 写完才能 spawn agent / 进入 prompt，否则读取到旧身份）。
#
# 失败策略：fail-open。任何步骤异常都不抛错，让 cc-connect 继续 spawn agent，
#          隔离会退化到 default 目录，但功能不挂。
#
# 字段解析由 ``python -m chatcopilot bot render-session-env`` 委托给当前 BotSpec 的
# PlatformAdapter；本脚本只负责跨进程搬运 env。这样 QQ/飞书等平台的 session key
# 形态不会泄漏到部署脚本。
set -uo pipefail

# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"
ccp_prepend_user_bins
ccp_load_env "CHATCOPILOT_|WORKSPACE_ROOT"
ccp_apply_bot_deploy_config

SESS_KEY="${CC_HOOK_SESSION_KEY:-${CC_SESSION_KEY:-}}"
if [ -z "$SESS_KEY" ]; then
    echo "[_session_env] CC_HOOK_SESSION_KEY/CC_SESSION_KEY both empty, skip" >&2
    exit 0
fi

USER_ID="${CC_HOOK_USER_ID:-}"
CHAT_ID="${CC_HOOK_CHAT_ID:-}"
CHAT_KIND="${CC_HOOK_CHAT_KIND:-${CC_HOOK_CHAT_TYPE:-${CC_HOOK_MESSAGE_TYPE:-${CC_HOOK_EVENT_TYPE:-}}}}"
# CC_HOOK_USER_NAME 是飞书显示名（如"示例用户"），用于 access_control 角色匹配。
# 注意：飞书可改名，重名也可能存在；access_control 模块会以 user_id 优先匹配。
USER_NAME="${CC_HOOK_USER_NAME:-}"

TARGET="/tmp/cc-sess-${SESS_KEY}.env"
PY="${CHATCOPILOT_ACP_PY:-$CCP_HOME_DEFAULT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
fi
BOT_SPEC="${CHATCOPILOT_BOT_SPEC:-}"

if [ -n "$PY" ] && [ -n "$BOT_SPEC" ] && [ -f "$BOT_SPEC" ]; then
    if "$PY" -m chatcopilot bot render-session-env \
        --bot "$BOT_SPEC" \
        --session-key "$SESS_KEY" \
        --user-id "$USER_ID" \
        --chat-id "$CHAT_ID" \
        --chat-kind "$CHAT_KIND" \
        --user-name "$USER_NAME" > "$TARGET" 2>>"/tmp/_session_env_errors.log"; then
        # shellcheck disable=SC1090
        source "$TARGET" 2>>"/tmp/_session_env_errors.log" || true
        echo "[_session_env] wrote $TARGET (user=${CHATCOPILOT_USER_ID:-} name=${CHATCOPILOT_USER_NAME:-} chat=${CHATCOPILOT_CHAT_KIND:-}/${CHATCOPILOT_CHAT_ID:-})" >&2
        exit 0
    fi
    echo "[_session_env] render-session-env failed, fallback to explicit hook fields" >&2
else
    echo "[_session_env] render-session-env unavailable (py=${PY:-?} bot=${BOT_SPEC:-?}), fallback to explicit hook fields" >&2
fi

[ -n "$CHAT_KIND" ] || CHAT_KIND="p2p"
# USER_NAME 可能含中文/空格，用 %q 做 shell-quote，保证 bot_wrapper.sh 的
# `set -a; source` 能安全 export 出去（裸 UTF-8 在 bash 下 OK，但含 ` $ "
# 等特殊字符的极端姓名会破坏 source）。
{
    printf 'export CHATCOPILOT_USER_ID=%q\n' "$USER_ID"
    printf 'export CHATCOPILOT_CHAT_ID=%q\n'  "$CHAT_ID"
    printf 'export CHATCOPILOT_CHAT_KIND=%q\n' "$CHAT_KIND"
    printf 'export CHATCOPILOT_USER_NAME=%q\n' "$USER_NAME"
} > "$TARGET" 2>>"/tmp/_session_env_errors.log" || {
    echo "[_session_env] failed to write $TARGET" >&2
    exit 0
}

# stderr 打印一行总结，cc-connect 主日志可见，便于排查
echo "[_session_env] wrote $TARGET (user=$USER_ID name=$USER_NAME chat=$CHAT_KIND/$CHAT_ID)" >&2

exit 0
