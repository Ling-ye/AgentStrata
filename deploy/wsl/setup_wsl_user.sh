#!/usr/bin/env bash
# setup_wsl_user.sh — 用户态部分。Cursor 会代你跑。
# 用法：bash ~/setup_wsl_user.sh
#   - 配 npm 全局 prefix 到 ~/.npm-global，免后续 sudo
#   - 装 cc-connect / lark-cli（不再装 cursor-agent CLI——已改用 ACP 协议自建 ACP server）
#   - 在 ~/ChatCopilot 下建 venv 并装 Python 依赖（含 src/chatcopilot/middleware/acp 的 ACP SDK）
#   - 准备工作目录骨架
# 幂等：可重复跑。
set -uo pipefail

C_INFO="\033[1;36m"
C_OK="\033[1;32m"
C_WARN="\033[1;33m"
C_ERR="\033[1;31m"
C_END="\033[0m"

log() { printf "${C_INFO}[INFO]${C_END} %s\n" "$*"; }
ok()  { printf "${C_OK}[OK]${C_END} %s\n" "$*"; }
skip(){ printf "${C_WARN}[SKIP]${C_END} %s\n" "$*"; }
warn(){ printf "${C_WARN}[WARN]${C_END} %s\n" "$*"; }
err() { printf "${C_ERR}[ERR]${C_END} %s\n" "$*"; }

need() { command -v "$1" >/dev/null 2>&1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/_load_env.sh" ]; then
    # shellcheck source=./_load_env.sh
    source "$SCRIPT_DIR/_load_env.sh"
    ccp_load_env "FEISHU_APP_ID|FEISHU_APP_SECRET|TAVILY_API_KEY|QQ_|CHATCOPILOT_"
    ccp_apply_bot_deploy_config
fi

# 根据 BotSpec 的 platform.type 决定要装的 cc-connect 通道与是否需要 lark-cli。
# 当前 BOT_SPEC 由 ccp_apply_bot_deploy_config 设置；若不存在则按多 bot.yaml 探测。
PLATFORM_TYPE_FOR_SETUP=""
_CANDIDATE_BOTS=()
if [ -n "${CHATCOPILOT_BOT_SPEC:-}" ] && [ -f "$CHATCOPILOT_BOT_SPEC" ]; then
    _CANDIDATE_BOTS+=("$CHATCOPILOT_BOT_SPEC")
fi
_CANDIDATE_DIR="${CHATCOPILOT_HOME:-$CCP_HOME_DEFAULT}/bots"
if [ -d "$_CANDIDATE_DIR" ]; then
    while IFS= read -r f; do
        _CANDIDATE_BOTS+=("$f")
    done < <(find "$_CANDIDATE_DIR" -mindepth 2 -maxdepth 2 -name bot.yaml 2>/dev/null)
fi
for _bot in "${_CANDIDATE_BOTS[@]}"; do
    [ -f "$_bot" ] || continue
    _t="$(BOT_SPEC="$_bot" python3 - <<'PY'
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
        current = line.split(":", 1)[0].strip(); continue
    if current == "platform" and raw[:1].isspace() and ":" in line:
        key, value = line.split(":", 1)
        if key.strip() == "type":
            print(value.strip().strip("\"'")); raise SystemExit(0)
print("")
PY
)"
    case "$_t" in
        qq) PLATFORM_TYPE_FOR_SETUP="qq" ;;
        feishu) [ -z "$PLATFORM_TYPE_FOR_SETUP" ] && PLATFORM_TYPE_FOR_SETUP="feishu" ;;
    esac
done
PLATFORM_TYPE_FOR_SETUP="${PLATFORM_TYPE_FOR_SETUP:-feishu}"
log "检测到 platform.type=$PLATFORM_TYPE_FOR_SETUP（多个实例时优先 qq，确保 cc-connect@beta 通道可用）"

if [ "$(id -u)" -eq 0 ]; then
    err "本脚本不要用 sudo 跑；它装的是用户态全局包。"
    exit 1
fi

# 1. 校验前置 (root 阶段)
if ! need node; then
    err "缺 node。请先跑：sudo bash ~/setup_wsl_root.sh"
    exit 1
fi
NODE_VER=$(node --version | sed 's/v//')
NODE_MAJ=${NODE_VER%%.*}
if [ "$NODE_MAJ" -lt 18 ] 2>/dev/null; then
    err "node 版本太旧（$(node --version)），cc-connect 要 18+。请重跑 root 阶段。"
    exit 1
fi
ok "Node.js $(node --version) / npm $(npm --version)"

# 2. npm 全局 prefix → ~/.npm-global
NPM_PREFIX="$HOME/.npm-global"
if [ "$(npm config get prefix 2>/dev/null)" != "$NPM_PREFIX" ]; then
    mkdir -p "$NPM_PREFIX"
    npm config set prefix "$NPM_PREFIX"
    ok "npm 全局 prefix 设为 $NPM_PREFIX"
else
    skip "npm 全局 prefix 已是 $NPM_PREFIX"
fi

# 把 npm prefix 和 Cursor agent 都写进 ~/.bashrc（幂等）
ensure_path_in_bashrc() {
    local marker="$1"
    local snippet="$2"
    if ! grep -qF "$marker" ~/.bashrc 2>/dev/null; then
        printf '\n# %s\n%s\n' "$marker" "$snippet" >> ~/.bashrc
        ok "~/.bashrc 注入 $marker"
    fi
}
ensure_path_in_bashrc "ChatCopilot: npm-global PATH" 'export PATH="$HOME/.npm-global/bin:$PATH"'

export PATH="$HOME/.npm-global/bin:$PATH"

# 3. cc-connect
# 通道选择：qq (OneBot / NapCat) 目前仅在 beta 通道发布；其它平台用 stable。
if [ "$PLATFORM_TYPE_FOR_SETUP" = "qq" ]; then
    CC_CONNECT_PKG="cc-connect@1.4.0-beta.3"
else
    CC_CONNECT_PKG="cc-connect"
fi
CC_CONNECT_USER_BIN="$NPM_PREFIX/bin/cc-connect"
if [ -x "$CC_CONNECT_USER_BIN" ]; then
    _ver="$("$CC_CONNECT_USER_BIN" --version 2>&1 | head -n1 || true)"
    skip "cc-connect 已就绪: $_ver"
    if [ "$PLATFORM_TYPE_FOR_SETUP" = "qq" ] && ! echo "$_ver" | grep -qF "1.4.0-beta.3"; then
        warn "platform.type=qq 当前固定 cc-connect 1.4.0-beta.3；检测到其它版本，"
        warn "  请显式安装：npm install -g cc-connect@1.4.0-beta.3"
    fi
else
    log "npm install -g $CC_CONNECT_PKG"
    if npm install -g "$CC_CONNECT_PKG"; then
        if [ -x "$CC_CONNECT_USER_BIN" ]; then
            ok "cc-connect: $("$CC_CONNECT_USER_BIN" --version 2>&1 | head -n1)"
        else
            warn "cc-connect 安装后仍找不到，请检查 PATH 是否包含 $NPM_PREFIX/bin"
        fi
    else
        warn "cc-connect 安装失败；可手动 npm i -g $CC_CONNECT_PKG 重试"
    fi
fi

# 4. [已删除] Cursor Agent CLI —— 改用 ACP 协议自建 AgentStrata ACP runtime，
#     不再依赖第三方 CLI 的不可覆盖内置 system prompt。

# 5. lark-cli（飞书表格下载用）—— 仅当本机至少有一个 platform.type=feishu 实例时才装
if [ "$PLATFORM_TYPE_FOR_SETUP" = "feishu" ]; then
    if need lark-cli; then
        skip "lark-cli 已就绪"
    else
        log "npm install -g @larksuite/cli"
        if npm install -g @larksuite/cli; then
            if need lark-cli; then
                ok "lark-cli 安装完成"
            else
                warn "lark-cli 装完后仍找不到，请检查 PATH"
            fi
        else
            warn "lark-cli 安装失败（不影响 cc-connect/MCP server 启动）"
        fi
    fi
else
    skip "lark-cli 安装：platform.type=$PLATFORM_TYPE_FOR_SETUP，无需飞书表格下载工具"
fi

prepare_lark_cli_config() {
    if [ "$PLATFORM_TYPE_FOR_SETUP" != "feishu" ]; then
        skip "跳过 lark-cli 凭证配置：platform.type=$PLATFORM_TYPE_FOR_SETUP"
        return 0
    fi
    if ! need lark-cli; then
        warn "跳过 lark-cli 凭证配置：lark-cli 不在 PATH"
        return 0
    fi
    if [ -z "${FEISHU_APP_ID:-}" ] || [ -z "${FEISHU_APP_SECRET:-}" ]; then
        warn "跳过 lark-cli 凭证配置：FEISHU_APP_ID / FEISHU_APP_SECRET 未设置"
        warn "  可补齐 $CCP_ENV_FILE 后重跑 bootstrap_wsl.sh"
        return 0
    fi

    python3 <<'PY'
import json
import os
from pathlib import Path

cfg_path = Path.home() / ".lark-cli" / "config.json"
if cfg_path.is_file():
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    apps = data.get("apps", []) if isinstance(data, dict) else []
    has_app = (
        isinstance(data, dict)
        and bool(data.get("appId"))
        and bool(data.get("appSecret"))
    ) or any(
        isinstance(app, dict) and app.get("appId") and app.get("appSecret")
        for app in apps
        if isinstance(app, dict)
    )
    if has_app:
        print("exists")
        raise SystemExit(0)

app_id = os.environ["FEISHU_APP_ID"].strip()
app_secret = os.environ["FEISHU_APP_SECRET"].strip()
cfg_path.parent.mkdir(parents=True, exist_ok=True)
cfg = {
    "apps": [
        {
            "appId": app_id,
            "appSecret": app_secret,
            "brand": "feishu",
            "lang": "zh",
            "users": [],
        }
    ]
}
cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
os.chmod(cfg_path.parent, 0o700)
os.chmod(cfg_path, 0o600)
print("created")
PY
    case "$?" in
        0)
            if python3 - <<'PY'
import json
from pathlib import Path
cfg_path = Path.home() / ".lark-cli" / "config.json"
data = json.loads(cfg_path.read_text(encoding="utf-8"))
apps = data.get("apps", []) if isinstance(data, dict) else []
ok = (
    isinstance(data, dict) and data.get("appId") and data.get("appSecret")
) or any(isinstance(app, dict) and app.get("appId") and app.get("appSecret") for app in apps)
raise SystemExit(0 if ok else 1)
PY
            then
                ok "lark-cli 应用凭证已就绪: ~/.lark-cli/config.json"
            else
                warn "lark-cli 凭证文件存在但结构异常，请检查 ~/.lark-cli/config.json"
            fi
            ;;
        *)
            warn "生成 lark-cli 凭证失败，请手动运行: lark-cli config init --new"
            ;;
    esac
}

prepare_lark_cli_config

# 6. Python venv + 依赖
MT_DIR="${CHATCOPILOT_DIR:-${CHATCOPILOT_HOME:-$HOME/ChatCopilot}}"
if [ ! -d "$MT_DIR" ]; then
    err "找不到 $MT_DIR；先用 rsync 同步代码"
    exit 1
fi

VENV_DIR="$MT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    log "创建 venv: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
log "安装 AgentStrata Agent 依赖（含 mcp SDK）"
pip install --quiet -r "$MT_DIR/src/chatcopilot/agent/requirements.txt"
log "安装 ACP middleware 依赖（含 agent-client-protocol —— ACP server 核心依赖）"
pip install --quiet -r "$MT_DIR/src/chatcopilot/middleware/acp/requirements.txt"
# 主项目 / 子项目的其它依赖
for req in \
    "$MT_DIR/requirements.txt"; do
    if [ -f "$req" ]; then
        log "安装 $req"
        pip install --quiet -r "$req" || warn "$req 装失败，继续"
    fi
done
log "以 editable 模式安装 AgentStrata src 包"
pip install --quiet -e "$MT_DIR"
log "检查 AgentStrata BotSpec 与 ACP 入口导入"
if ! python -c "import sys; sys.path.insert(0, '$MT_DIR/src'); from chatcopilot.run import main; from chatcopilot.middleware.acp.server import main as acp_main; print('ok')" >/dev/null; then
    err "AgentStrata 运行时导入失败：请确认 src/chatcopilot 包、src/chatcopilot/middleware/acp/requirements.txt 和 BotSpec 已同步"
    exit 1
fi
deactivate
ok "Python venv 就绪: $VENV_DIR"

# 7. 工作目录骨架
WS_ROOT="${CHATCOPILOT_WORKSPACE_ROOT:-$HOME/chatcopilot-workspaces}"
mkdir -p "$WS_ROOT/default/downloads" \
         "$WS_ROOT/default/results" \
         "$WS_ROOT/default/uploads" \
         "$WS_ROOT/default/.cc-connect/attachments"
ok "工作目录: $WS_ROOT/default"

# 7b. 运行日志目录 + 部署脚本可执行位
LOG_DIR="${CHATCOPILOT_LOG_DIR:-$HOME/chatcopilot-logs}"
mkdir -p "$LOG_DIR"
ok "运行日志目录: $LOG_DIR"

for sh in start.sh status.sh dump.sh \
          _apply_config.sh _load_env.sh _session_env.sh _stop_cc.sh \
          bot_wrapper.sh; do
    f="$MT_DIR/deploy/wsl/$sh"
    [ -f "$f" ] && chmod +x "$f"
done

# 8. cc-connect 配置目录
mkdir -p ~/.cc-connect
ok "~/.cc-connect 已创建"

echo
ok "用户态全部就绪。下一步由 Cursor 写配置文件并跑烟雾测试。"
echo "  - 飞书机器人入口：python -m chatcopilot run --bot bots/<bot-id>/bot.yaml（ACP server，stdio JSON-RPC）"
echo "  - system prompt 由 BotSpec prompts + 运行时身份上下文装配，不再走 .cursor/rules/"
echo "  - python -m chatcopilot 是 legacy pywebview 调试入口，不影响飞书机器人"
