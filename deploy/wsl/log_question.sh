#!/usr/bin/env bash
# log_question.sh — cc-connect message.received 钩子脚本
#
# 由 cc-connect 在收到用户消息时异步调用。从 CC_HOOK_* 环境变量读取
# 时间、使用人与问题原文会写入精简日志；完整 CC_HOOK_* 上下文会写入 raw 日志。
#
# 调用时机：cc-connect [[hooks]] event = "message.received"
# 输出文件：
# - 精简日志：~/chatcopilot-logs/<YYYY-MM-DD>.log
# - 原始日志：~/chatcopilot-logs/raw/<YYYY-MM-DD>.log
# 错误处理：写失败时落盘 _hook_errors.log + 同时输出到 stderr
#           cc-connect 主日志可见，便于运维发现
# 依赖：纯 bash，无 jq / python 依赖
#
# 字段名说明：当前 cc-connect 实测消息正文在 CC_HOOK_CONTENT；保留
# CC_HOOK_MESSAGE 作为旧版本兼容兜底。
set -uo pipefail

LOG_DIR="${CHATCOPILOT_LOG_DIR:-$HOME/chatcopilot-logs}"
RAW_LOG_DIR="$LOG_DIR/raw"
mkdir -p "$LOG_DIR" "$RAW_LOG_DIR" 2>/dev/null || {
    printf '[log_question.sh] failed to mkdir %s\n' "$LOG_DIR" >&2
    exit 0   # fail-open: hook 失败不影响 cc-connect 主流程
}

TARGET="$LOG_DIR/$(date +%F).log"
RAW_TARGET="$RAW_LOG_DIR/$(date +%F).log"

# 首次部署 sanity check：把 _DEBUG_DUMP=1 设到 cc-connect agent 的 env 里
# 或临时取消下面这段注释，跑一次确认 CC_HOOK_* 字段名再删掉
# if [ "${CHATCOPILOT_LOG_DEBUG:-0}" = "1" ]; then
#     env | grep -E '^CC_HOOK_' | sort >> "$LOG_DIR/_first_run_dump.log"
# fi

msg="${CC_HOOK_CONTENT:-${CC_HOOK_MESSAGE:-}}"
msg="${msg//$'\n'/\\n}"
msg="${msg//$'\r'/}"

ts="$(date '+%Y-%m-%d %H:%M:%S%:z')"
user_name="${CC_HOOK_USER_NAME:-}"
user_id="${CC_HOOK_USER_ID:-}"
user="${user_name:-${user_id:-未知用户}}"

if ! printf '[%s] | %s | %s\n' \
        "$ts" "$user" "$msg" \
        >> "$TARGET" 2>>"$LOG_DIR/_hook_errors.log"; then
    printf '[log_question.sh] failed to write %s\n' "$TARGET" >&2
fi

{
    printf -- '--- [%s] raw message.received hook ---\n' "$ts"
    env | sort | while IFS= read -r line; do
        case "$line" in
            CC_HOOK_*) printf '%s\n' "$line" ;;
        esac
    done
    printf '\n'
} >> "$RAW_TARGET" 2>>"$LOG_DIR/_hook_errors.log" || {
    printf '[log_question.sh] failed to write %s\n' "$RAW_TARGET" >&2
}

exit 0
