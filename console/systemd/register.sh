#!/usr/bin/env bash
# register.sh — 一次性把某个（或全部）bot 注册为 systemd --user 服务
#
# 现有 bot 当初是前台 start.sh 跑的，没有 systemd unit。运维控制台用
# `systemctl --user start|stop|restart chatcopilot@<id>` 管理它们，前提是：
#   1. 安装模板 unit chatcopilot@.service 到 ~/.config/systemd/user/
#   2. 为每个实例写 ~/.config/chatcopilot-console/<id>.env（含 CCP_WSL_HOME）
#   3. 开 lingering，让你关掉终端后服务仍存活、WSL 一启动就自动拉起
#
# 这一步独立于控制台日常操作，只需每个 bot 跑一次（或新增/改 wsl_home 时再跑）。
#
# 用法（WSL 终端）：
#   bash register.sh                         # 注册仓库 bots/ 下所有 bot
#   bash register.sh lingye-copilot-qq        # 只注册指定实例
#   bash register.sh --enable lingye-copilot-qq   # 注册并设为开机（WSL 启动）自启
#   bash register.sh --list                  # 列出当前已注册的 chatcopilot@ 服务
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." >/dev/null 2>&1 && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
CONSOLE_CONF_DIR="$HOME/.config/chatcopilot-console"
TEMPLATE_SRC="$(dirname "$0")/chatcopilot@.service"
CODE_WORKER_TEMPLATE_SRC="$(dirname "$0")/chatcopilot-code-worker@.service"

ENABLE=0
LIST=0
TARGETS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --enable) ENABLE=1; shift ;;
        --list) LIST=1; shift ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
        -*) echo "[ERR] 未知参数：$1" >&2; exit 2 ;;
        *) TARGETS+=("$1"); shift ;;
    esac
done

info() { printf "\033[1;36m[*]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ERR]\033[0m %s\n" "$*" >&2; }

if [ "$LIST" = 1 ]; then
    systemctl --user list-units --type=service --all 2>/dev/null | grep -E 'chatcopilot@' || echo "（暂无 chatcopilot@ 服务）"
    exit 0
fi

# ---- 读取某个 bot.yaml 的 deploy 字段（与 _load_env.sh 同源的极简解析）----
read_deploy_value() {
    local bot="$1" name="$2"
    python3 - "$bot" "$name" <<'PY'
import sys
from pathlib import Path
bot = Path(sys.argv[1]); want = sys.argv[2]
section = ""
for raw in bot.read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw.split("#", 1)[0].rstrip()
    if not line.strip():
        continue
    if not raw[:1].isspace() and ":" in line:
        section = line.split(":", 1)[0].strip(); continue
    if section == "deploy" and raw[:1].isspace() and ":" in line:
        key, value = line.split(":", 1)
        if key.strip() == want:
            print(value.strip().strip('"').strip("'")); break
PY
}

expand_tilde() {
    local v="$1"
    case "$v" in
        "~") echo "$HOME" ;;
        # 注意：${v#~/} 里的 ~/ 会被 bash 做 tilde 展开成 $HOME/，导致剥错前缀；
        # 必须加引号 "~/" 关闭 tilde 展开，才能真正剥掉字面量 ~/。
        "~/"*) echo "$HOME/${v#"~/"}" ;;
        *) echo "$v" ;;
    esac
}

# ---- 收集要注册的实例 ----
declare -a BOT_FILES=()
if [ "${#TARGETS[@]}" -gt 0 ]; then
    for t in "${TARGETS[@]}"; do
        f="$REPO_ROOT/bots/$t/bot.yaml"
        if [ -f "$f" ]; then BOT_FILES+=("$f"); else err "找不到 bot：$f"; exit 1; fi
    done
else
    while IFS= read -r f; do BOT_FILES+=("$f"); done < <(find "$REPO_ROOT/bots" -mindepth 2 -maxdepth 2 -name bot.yaml 2>/dev/null | sort)
fi

if [ "${#BOT_FILES[@]}" -eq 0 ]; then
    err "没有发现任何 bots/*/bot.yaml"
    exit 1
fi

# ---- 安装模板 unit ----
if ! mkdir -p "$USER_UNIT_DIR" "$CONSOLE_CONF_DIR"; then
    err "创建 systemd 或控制台配置目录失败"
    exit 1
fi
if [ -L "$CONSOLE_CONF_DIR" ]; then
    err "refusing symlink console config directory: $CONSOLE_CONF_DIR"
    exit 1
fi
if ! chmod 700 "$CONSOLE_CONF_DIR"; then
    err "failed to secure console config directory: $CONSOLE_CONF_DIR"
    exit 1
fi
if [ ! -f "$TEMPLATE_SRC" ] || [ ! -f "$CODE_WORKER_TEMPLATE_SRC" ]; then
    err "找不到模板 unit：$TEMPLATE_SRC 或 $CODE_WORKER_TEMPLATE_SRC"
    exit 1
fi
if ! cp -f "$TEMPLATE_SRC" "$USER_UNIT_DIR/chatcopilot@.service"; then
    err "安装模板 unit 失败：$USER_UNIT_DIR/chatcopilot@.service"
    exit 1
fi
if ! cp -f "$CODE_WORKER_TEMPLATE_SRC" "$USER_UNIT_DIR/chatcopilot-code-worker@.service"; then
    err "安装代码任务 worker unit 失败：$USER_UNIT_DIR/chatcopilot-code-worker@.service"
    exit 1
fi
ok "已安装模板 unit：$USER_UNIT_DIR/chatcopilot@.service"
ok "已安装代码任务 worker unit：$USER_UNIT_DIR/chatcopilot-code-worker@.service"

# ---- lingering ----
if command -v loginctl >/dev/null 2>&1; then
    linger="$(loginctl show-user "$USER" -p Linger 2>/dev/null | cut -d= -f2)"
    if [ "$linger" != "yes" ]; then
        if loginctl enable-linger "$USER" 2>/dev/null; then
            ok "已开启 lingering（关终端后服务仍存活）"
        else
            warn "无法自动开启 lingering，请手动执行：sudo loginctl enable-linger $USER"
        fi
    else
        ok "lingering 已开启"
    fi
fi

# ---- 逐实例写 env 文件 ----
for bot in "${BOT_FILES[@]}"; do
    iid="$(read_deploy_value "$bot" instance_id)"
    [ -z "$iid" ] && iid="$(basename "$(dirname "$bot")")"
    wsl_home_raw="$(read_deploy_value "$bot" wsl_home)"
    [ -z "$wsl_home_raw" ] && wsl_home_raw="~/ChatCopilot-$iid"
    wsl_home="$(expand_tilde "$wsl_home_raw")"
    bot_rel="${bot#"$REPO_ROOT"/}"
    deployed_bot="$wsl_home/$bot_rel"
    log_dir_raw="$(read_deploy_value "$bot" log_dir)"
    [ -z "$log_dir_raw" ] && log_dir_raw="~/chatcopilot-logs/$iid"
    log_dir="$(expand_tilde "$log_dir_raw")"
    workspace_root_raw="$(read_deploy_value "$bot" workspace_root)"
    [ -z "$workspace_root_raw" ] && workspace_root_raw="~/chatcopilot-workspaces/$iid"
    workspace_root="$(expand_tilde "$workspace_root_raw")"

    conf="$CONSOLE_CONF_DIR/$iid.env"
    if ! {
        echo "# generated by console/systemd/register.sh"
        echo "CCP_WSL_HOME=$wsl_home"
        echo "CHATCOPILOT_INSTANCE_ID=$iid"
        echo "CHATCOPILOT_BOT_SPEC=$deployed_bot"
    } > "$conf"; then
        err "写入实例配置失败：$conf"
        exit 1
    fi
    if ! chmod 600 "$conf"; then
        err "收紧实例配置权限失败：$conf"
        exit 1
    fi
    ok "实例 $iid -> $conf (CCP_WSL_HOME=$wsl_home)"

    worker_conf="$CONSOLE_CONF_DIR/$iid-code-worker.env"
    local_env="$(dirname "$bot")/local.env"
    if ! CHATCOPILOT_REGISTER_BOT="$bot" \
        CHATCOPILOT_REGISTER_LOCAL_ENV="$local_env" \
        CHATCOPILOT_REGISTER_REPO="$REPO_ROOT" \
        CHATCOPILOT_REGISTER_INSTANCE="$iid" \
        CHATCOPILOT_REGISTER_RUNTIME="$wsl_home" \
        CHATCOPILOT_REGISTER_LOG_DIR="$log_dir" \
        CHATCOPILOT_REGISTER_WORKSPACE_ROOT="$workspace_root" \
        CHATCOPILOT_REGISTER_WORKER_ENV="$worker_conf" \
        python3 <<'PY'
import json
import os
import re
import stat
import tempfile
from pathlib import Path

allowed = {
    "CHATCOPILOT_CODEX_BIN",
    "CHATCOPILOT_CODEX_BOT_HOME",
    "CHATCOPILOT_CODE_TASK_GITHUB_REPOSITORY",
    "CHATCOPILOT_CODE_TASK_GITHUB_TOKEN",
    "CHATCOPILOT_CODE_TASK_GIT_AUTHOR_NAME",
    "CHATCOPILOT_CODE_TASK_GIT_AUTHOR_EMAIL",
    "CHATCOPILOT_CODE_TASK_QUICK_COMMAND",
    "CHATCOPILOT_CODE_TASK_FULL_COMMAND",
    "CHATCOPILOT_CODE_TASK_TIMEOUT_SECONDS",
    "CHATCOPILOT_CODE_TASK_MEMORY_MAX_BYTES",
    "CHATCOPILOT_CODE_TASK_CPU_QUOTA_PERCENT",
    "CHATCOPILOT_CODE_TASK_TASKS_MAX",
    "CHATCOPILOT_CODE_TASK_DISK_MAX_BYTES",
    "CHATCOPILOT_CODE_TASK_HEARTBEAT_SECONDS",
    "CHATCOPILOT_CODE_TASK_PROGRESS_NOTIFY_SECONDS",
    "CHATCOPILOT_CODE_TASK_CANCEL_GRACE_SECONDS",
    "CHATCOPILOT_LIMIT_DIR",
    "CHATCOPILOT_LOG_DIR",
    "CHATCOPILOT_WORKSPACE_ROOT",
}
instance_code_policy = re.compile(
    r"CHATCOPILOT_[A-Z0-9]+(?:_[A-Z0-9]+)*_CODE_"
    r"(?:PROVIDER|MODEL|REASONING_EFFORT|PROFILES_JSON|TASK_PROFILE)"
)
values = {}


def expand_home_prefix(value):
    home = str(Path.home())
    if value in {"~", "$HOME", "${HOME}"}:
        return home
    for prefix in ("~/", "$HOME/", "${HOME}/"):
        if value.startswith(prefix):
            return f"{home}/{value[len(prefix):]}"
    return value


source = Path(os.environ["CHATCOPILOT_REGISTER_LOCAL_ENV"])
if source.is_file():
    for raw in source.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        key = re.sub(r"^export\s+", "", key)
        if (
            not re.fullmatch(r"[A-Z0-9_]+", key)
            or (
                key not in allowed
                and instance_code_policy.fullmatch(key) is None
            )
        ):
            continue
        value = value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        values[key] = (
            value
            if key == "CHATCOPILOT_CODE_TASK_GITHUB_TOKEN"
            else expand_home_prefix(value)
        )


token = values.pop("CHATCOPILOT_CODE_TASK_GITHUB_TOKEN", "").strip()

values.update(
    {
        "CHATCOPILOT_SOURCE_ROOT": os.environ["CHATCOPILOT_REGISTER_REPO"],
        "CHATCOPILOT_RUNTIME_ROOT": os.environ["CHATCOPILOT_REGISTER_RUNTIME"],
        "CHATCOPILOT_HOME": os.environ["CHATCOPILOT_REGISTER_RUNTIME"],
        "CHATCOPILOT_INSTANCE_ID": os.environ["CHATCOPILOT_REGISTER_INSTANCE"],
        "CHATCOPILOT_LOG_DIR": os.environ["CHATCOPILOT_REGISTER_LOG_DIR"],
        "CHATCOPILOT_SOURCE_BOT_SPEC": os.environ["CHATCOPILOT_REGISTER_BOT"],
    }
)
values["CHATCOPILOT_WORKSPACE_ROOT"] = os.environ[
    "CHATCOPILOT_REGISTER_WORKSPACE_ROOT"
]
target = Path(os.environ["CHATCOPILOT_REGISTER_WORKER_ENV"])
token_target = target.with_name(
    f"{os.environ['CHATCOPILOT_REGISTER_INSTANCE']}-code-worker-github.token"
)


def require_private_parent(path):
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path.parent, flags)
    except OSError as exc:
        raise RuntimeError(
            f"worker credential/config directory is unavailable: {path.parent}"
        ) from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise RuntimeError(
                "worker credential/config directory must be owner-only mode 0700"
            )
    finally:
        os.close(fd)


def atomic_private_text(path, content):
    if path.is_symlink():
        raise RuntimeError(f"refusing symlink credential/config target: {path.name}")
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def valid_private_token_file(path):
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            return False
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            existing = stream.read().strip()
        return re.fullmatch(r"[A-Za-z0-9_-]{20,255}", existing) is not None
    finally:
        if fd >= 0:
            os.close(fd)


require_private_parent(target)


delivery_fields = (
    "CHATCOPILOT_CODE_TASK_GITHUB_REPOSITORY",
    "CHATCOPILOT_CODE_TASK_GIT_AUTHOR_NAME",
    "CHATCOPILOT_CODE_TASK_GIT_AUTHOR_EMAIL",
)
delivery_configured = bool(token) or any(values.get(name) for name in delivery_fields)
if delivery_configured:
    missing = [name for name in delivery_fields if not values.get(name)]
    if missing:
        raise RuntimeError(
            "incomplete code-task GitHub configuration: " + ", ".join(missing)
        )
    if token:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,255}", token):
            raise RuntimeError("CHATCOPILOT_CODE_TASK_GITHUB_TOKEN is malformed")
        atomic_private_text(token_target, token + "\n")
    elif not valid_private_token_file(token_target):
        raise RuntimeError(
            "GitHub token is missing and no valid existing 0600 token file exists"
        )
    values["CHATCOPILOT_CODE_TASK_GITHUB_TOKEN_FILE"] = str(token_target)

atomic_private_text(
    target,
    "# generated by console/systemd/register.sh; no platform credentials\n"
    + "".join(f"{key}={json.dumps(value)}\n" for key, value in sorted(values.items())),
)
PY
    then
        err "生成代码任务 worker 环境失败：$worker_conf"
        exit 1
    fi
    ok "实例 $iid 代码任务 worker 环境 -> $worker_conf"

    if [ ! -d "$wsl_home/deploy/wsl" ]; then
        warn "  $wsl_home 还未部署（缺 deploy/wsl）。先在控制台点「更新代码并重启」把代码同步过去。"
    fi
done

if ! systemctl --user daemon-reload; then
    err "systemctl --user daemon-reload 失败（检查 XDG_RUNTIME_DIR）"
    exit 1
fi
ok "systemd --user 已 reload"

if [ "$ENABLE" = 1 ]; then
    for bot in "${BOT_FILES[@]}"; do
        iid="$(read_deploy_value "$bot" instance_id)"
        [ -z "$iid" ] && iid="$(basename "$(dirname "$bot")")"
        systemctl --user enable "chatcopilot@$iid" 2>/dev/null \
            && ok "已设置开机自启：chatcopilot@$iid" \
            || warn "enable chatcopilot@$iid 失败"
        systemctl --user enable "chatcopilot-code-worker@$iid" 2>/dev/null \
            && ok "已设置开机自启：chatcopilot-code-worker@$iid" \
            || warn "enable chatcopilot-code-worker@$iid 失败"
    done
fi

echo
ok "注册完成。控制台即可用 systemctl --user start/stop/restart chatcopilot@<id> 管理这些实例。"
