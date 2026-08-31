#!/usr/bin/env bash
# bootstrap_wsl.sh — 在 WSL 实例副本中重建运行时（venv + 依赖 + 配置渲染）。
#
# 典型上下文：控制台「更新代码并重启」已经把 WSL 源仓同步到实例副本，
# 现在需要重建 venv 与 cc-connect 配置，跑这一条命令就能回到
# "可以 bash start.sh 启动"的状态。
#
# 用法：
#   bash bootstrap_wsl.sh              # 默认：locked installer + _apply_config.sh 全跑
#   bash bootstrap_wsl.sh --skip-apply # 只重建 venv，不渲染 cc-connect 配置
#   bash bootstrap_wsl.sh -h | --help
#
# 设计要点：
# - 仓库自我校验：如果 ~/ChatCopilot/ 不是合法仓库（缺关键文件），早死给清晰提示
# - 运行时信任边界：先 frozen reconcile，再用项目 Python 解析 BotSpec 和 env
# - 只编排 install_wsl_env.sh / _apply_config.sh，自身不重复实现逻辑
# - 不启动服务：start.sh 是前台进程，必须在常驻 WSL 终端里手跑

set -uo pipefail

# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"

# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
SKIP_APPLY=0

usage() { sed -n '2,15p' "$0"; }

for arg in "$@"; do
    case "$arg" in
        --skip-apply) SKIP_APPLY=1 ;;
        -h|--help)    usage; exit 0 ;;
        *)
            echo "[ERR] 未知参数：$arg（用 --help 看用法）" >&2
            exit 2 ;;
    esac
done

# ----------------------------------------------------------------------------
# 颜色
# ----------------------------------------------------------------------------
if [ -t 1 ]; then
    C_INFO=$'\033[1;36m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'
    C_ERR=$'\033[1;31m';  C_BOLD=$'\033[1m';  C_DIM=$'\033[2m'; C_END=$'\033[0m'
else
    C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_BOLD=""; C_DIM=""; C_END=""
fi
step() { printf "%s[%s]%s %s\n" "$C_INFO" "$(date +%H:%M:%S)" "$C_END" "$*"; }
ok()   { printf "%s[OK]%s %s\n"   "$C_OK"   "$C_END" "$*"; }
warn() { printf "%s[WARN]%s %s\n" "$C_WARN" "$C_END" "$*"; }
err()  { printf "%s[ERR]%s %s\n"  "$C_ERR"  "$C_END" "$*" >&2; }

# ----------------------------------------------------------------------------
# 路径与校验
# ----------------------------------------------------------------------------
MT_HOME="${CHATCOPILOT_HOME:-$CCP_HOME_DEFAULT}"
DEPLOY_DIR="$MT_HOME/deploy/wsl"
INSTALL_SCRIPT="$DEPLOY_DIR/install_wsl_env.sh"
APPLY_SCRIPT="$DEPLOY_DIR/_apply_config.sh"

echo
printf "%s=== AgentStrata · WSL bootstrap ===%s\n" "$C_BOLD" "$C_END"
echo

if [ ! -d "$MT_HOME" ]; then
    err "仓库目录不存在：$MT_HOME"
    err "  请先在控制台点「更新代码并重启」，或在 WSL 源仓运行 update_instance.sh。"
    exit 1
fi

if [ ! -f "$INSTALL_SCRIPT" ]; then
    err "缺关键脚本：$INSTALL_SCRIPT"
    err "  仓库结构异常或没同步完整。建议重新 sync。"
    exit 1
fi

if [ ! -f "$APPLY_SCRIPT" ] && [ "$SKIP_APPLY" -eq 0 ]; then
    err "缺 _apply_config.sh：$APPLY_SCRIPT"
    err "  仓库结构异常或没同步完整。如不想渲染配置可加 --skip-apply。"
    exit 1
fi

step "仓库就位：$MT_HOME"

# ----------------------------------------------------------------------------
# 1) 重建 venv + 装 Python 依赖
# ----------------------------------------------------------------------------
echo
step "[1/2] 通过 uv.lock 同步隔离 Python/Node/cc-connect 运行时"
echo
if ! bash "$INSTALL_SCRIPT" --no-system-packages --venv "$MT_HOME/.venv" --no-verify; then
    err "install_wsl_env.sh 执行失败"
    err "  请检查 uv/Node 制品下载、校验和或 uv.lock 同步错误"
    exit 1
fi
ok "venv 与依赖已就位：$MT_HOME/.venv"

# BotSpec 与运行时 env 的解析依赖项目 Python；只能在上面的 frozen
# reconcile 成功后调用，避免执行实例目录中尚未校验的既存 venv。
ccp_apply_bot_deploy_config
ccp_load_env "FEISHU_APP_ID|FEISHU_APP_SECRET|TAVILY_API_KEY|CHATCOPILOT_|WORKSPACE_ROOT"
ccp_apply_bot_deploy_config
ENV_FILE="$CCP_ENV_FILE"

# 机密 env 校验
if [ ! -f "$ENV_FILE" ]; then
    warn "$ENV_FILE 不存在（机密配置缺失，机器人会无法启动）。"
    echo
    _bot_env_rel=""
    if [ -n "${CHATCOPILOT_BOT_SPEC:-}" ]; then
        _bot_id="$(basename "$(dirname "$CHATCOPILOT_BOT_SPEC")")"
        _bot_env_rel="bots/${_bot_id}/local.env.example"
    fi
    if [ -n "$_bot_env_rel" ] && [ -f "$MT_HOME/$_bot_env_rel" ]; then
        echo "  当前实例使用 Bot 自己的 LLM 前缀与平台配置。"
        echo "  请回到源码仓，复制对应模板并填写真实值："
        echo
        echo "    cp $_bot_env_rel ${_bot_env_rel%.example}"
        echo "    chmod 600 ${_bot_env_rel%.example}"
        echo
        echo "  填好后生成运行时 env 并重新部署："
        echo "    bash deploy/wsl/update_instance.sh --instance ${CHATCOPILOT_INSTANCE_ID:-$_bot_id}"
        echo
        err "请先完成 bot-local env，再重新部署实例。"
    else
        echo "  请在 WSL 里执行以下命令生成模板，再把真实值填进去（chmod 600 已自动设）："
        echo
        cat <<EOF
${C_DIM}    cat > $ENV_FILE <<'ENVEOF'
# 机器人运行时机密配置（不进 git）

# 飞书 App 凭据（必填）
export FEISHU_APP_ID="cli_xxxxxxxxxxxxxxxx"
export FEISHU_APP_SECRET="xxxxxxxxxxxxxxxxxxxxxxxx"

# LiteLLM 虚拟密钥（必填；src/chatcopilot/middleware/acp 走它对接模型）
export CHATCOPILOT_CHAT_API_KEY="sk-xxxxxxxx"

# 可选：覆盖默认 LiteLLM 网关 / 模型 ID
# export CHATCOPILOT_CHAT_BASE_URL="https://api.example.com/v1"
# export CHATCOPILOT_CHAT_MODEL="dashscope/deepseek-v4-pro"

# 可选：把额外 Owner 的飞书 open_id 追加进权限白名单（逗号分隔，姓名匹配兜底外的加固）
# export CHATCOPILOT_ADD_OWNER_IDS="ou_xxxxxxxxxxxxxxxx"

# 可选：dump.sh 输出根，默认写到 ~/ChatCopilot/_wsl_debug/<timestamp>/
# export CHATCOPILOT_DUMP_ROOT="$HOME/ChatCopilot"
ENVEOF
    chmod 600 $ENV_FILE${C_END}
EOF
        echo
        err "请补完机密后重跑 bash bootstrap_wsl.sh。"
    fi
    exit 1
fi

# env 权限收紧（如果用户改名或重新生成后权限重置）
if [ "$(stat -c %a "$ENV_FILE" 2>/dev/null || echo 600)" != "600" ]; then
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    warn "$ENV_FILE 权限已收紧到 600"
fi
ok "机密 env 就位：$ENV_FILE"

# ----------------------------------------------------------------------------
# 2) 渲染 cc-connect 配置
# ----------------------------------------------------------------------------
if [ "$SKIP_APPLY" -eq 1 ]; then
    echo
    warn "[2/2] 已 --skip-apply：未渲染 cc-connect 配置"
    warn "  之后跑 bash start.sh --apply-config 再渲染"
else
    echo
    step "[2/2] 渲染 cc-connect 配置（~/.cc-connect/config.toml）"
    echo
    if ! bash "$APPLY_SCRIPT"; then
        err "_apply_config.sh 执行失败"
        err "  常见原因：FEISHU_APP_ID / FEISHU_APP_SECRET 没设（看上面错误信息）"
        exit 1
    fi
    ok "cc-connect 配置已渲染"
fi

# ----------------------------------------------------------------------------
# 总结
# ----------------------------------------------------------------------------
echo
ok "bootstrap 完成"
echo
printf "%s下一步：%s\n" "$C_BOLD" "$C_END"
echo "  推荐在运维控制台（http://localhost:8910）点「启动」拉起本实例（systemd 托管，开机自启）。"
echo "  等价命令：bash console/scripts/ctl.sh start <instance-id>"
echo "  健康检查：bash $DEPLOY_DIR/status.sh"
