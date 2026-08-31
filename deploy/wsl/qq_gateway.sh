#!/usr/bin/env bash
# qq_gateway.sh — manage the local NapCat gateway for QQ/OneBot instances.
#
# Usage:
#   bash deploy/wsl/qq_gateway.sh bootstrap --instance lingye-copilot-qq
#   bash deploy/wsl/qq_gateway.sh sync-token --instance lingye-copilot-qq
#   bash deploy/wsl/qq_gateway.sh start --instance lingye-copilot-qq
#   bash deploy/wsl/qq_gateway.sh restart --instance lingye-copilot-qq
#   bash deploy/wsl/qq_gateway.sh status --instance lingye-copilot-qq
#   bash deploy/wsl/qq_gateway.sh logs --instance lingye-copilot-qq
set -uo pipefail

ACTION="${1:-}"
[ $# -gt 0 ] && shift || true
INSTANCE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --instance|-i) INSTANCE="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "[ERR] 未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

ok()   { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
info() { printf "\033[1;36m[*]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ERR]\033[0m %s\n" "$*" >&2; }

if [ -z "$ACTION" ]; then
    err "必须指定动作：bootstrap|sync-token|start|restart|status|logs"
    exit 2
fi
if [ "$ACTION" != "bootstrap" ] && [ "$ACTION" != "sync-token" ] && [ "$ACTION" != "start" ] && [ "$ACTION" != "restart" ] && [ "$ACTION" != "status" ] && [ "$ACTION" != "logs" ]; then
    err "不支持的动作：$ACTION"
    exit 2
fi
if [ -z "$INSTANCE" ]; then
    err "必须指定 --instance <id>"
    exit 2
fi
if [ "${#INSTANCE}" -lt 2 ] || [ "${#INSTANCE}" -gt 63 ] \
    || [[ ! "$INSTANCE" =~ ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ ]]; then
    err "--instance 必须为 2–63 字符、以小写字母开头的 kebab-case"
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
export CHATCOPILOT_BOT_SPEC="$REPO_ROOT/bots/$INSTANCE/bot.yaml"
export CHATCOPILOT_INSTANCE_ID="$INSTANCE"

if [ ! -f "$CHATCOPILOT_BOT_SPEC" ]; then
    err "找不到 BotSpec: $CHATCOPILOT_BOT_SPEC"
    exit 1
fi

# shellcheck source=./_load_env.sh
source "$SCRIPT_DIR/_load_env.sh"
ccp_apply_bot_deploy_config
ccp_load_env "QQ_|CHATCOPILOT_|WORKSPACE_ROOT"

LOCAL_CONFIG="$REPO_ROOT/bots/$INSTANCE/local.env"
CONTAINER="napcat-$INSTANCE"
QQ_WS_URL="${QQ_WS_URL:-ws://127.0.0.1:3001}"
QQ_WEBUI_PORT="${QQ_WEBUI_PORT:-6099}"
DEFAULT_NAPCAT_IMAGE="mlikiowa/napcat-docker@sha256:0b4b24114089bfbbefd4729ad08b50a6b9d67044aec674809ede3cf7521c4431"
NAPCAT_IMAGE="${NAPCAT_IMAGE:-$DEFAULT_NAPCAT_IMAGE}"
NAPCAT_DISABLE_BYPASS="${NAPCAT_DISABLE_BYPASS:-1}"
NAPCAT_DISABLE_MULTI_PROCESS="${NAPCAT_DISABLE_MULTI_PROCESS:-1}"
NAPCAT_SHM_SIZE="${NAPCAT_SHM_SIZE:-512m}"
NAPCAT_QQ_DATA_VOLUME="${NAPCAT_QQ_DATA_VOLUME:-napcat-${INSTANCE}-qq-data}"
NAPCAT_CONFIG_VOLUME="${NAPCAT_CONFIG_VOLUME:-napcat-${INSTANCE}-config}"

NAPCAT_SHM_BYTES=""

parse_napcat_shm_size() {
    local value="$1" number_text suffix multiplier max_bytes="9223372036854775807"
    if [[ ! "$value" =~ ^([1-9][0-9]*)([bBkKmMgG]?)$ ]]; then
        err "NAPCAT_SHM_SIZE 必须是正整数，后缀只能是 b/k/m/g（例如 512m）。"
        return 1
    fi

    number_text="${BASH_REMATCH[1]}"
    suffix="${BASH_REMATCH[2],,}"
    if [ "${#number_text}" -gt 19 ] \
        || { [ "${#number_text}" -eq 19 ] && [[ "$number_text" > "$max_bytes" ]]; }; then
        err "NAPCAT_SHM_SIZE 超出支持的字节范围。"
        return 1
    fi

    case "$suffix" in
        ""|b) multiplier=1 ;;
        k) multiplier=1024 ;;
        m) multiplier=1048576 ;;
        g) multiplier=1073741824 ;;
    esac
    local number=$((10#$number_text))
    if [ "$number" -gt $((max_bytes / multiplier)) ]; then
        err "NAPCAT_SHM_SIZE 超出支持的字节范围。"
        return 1
    fi
    NAPCAT_SHM_BYTES=$((number * multiplier))
}

if [[ ! "$NAPCAT_IMAGE" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]]; then
    err "NAPCAT_IMAGE 必须是不可变的 name@sha256:<64hex> 引用。"
    exit 1
fi

if [[ ! "${QQ_ACCOUNT:-}" =~ ^[0-9]+$ ]]; then
    err "QQ_ACCOUNT 必须是纯数字的稳定 QQ ID。请先填写 bots/$INSTANCE/local.env，并运行：bash deploy/wsl/update_instance.sh --instance $INSTANCE"
    exit 1
fi

if ! parse_napcat_shm_size "$NAPCAT_SHM_SIZE"; then
    exit 1
fi

BOUNDARY_PY="$REPO_ROOT/.venv/bin/python"
if [ "$ACTION" != "logs" ] && [ ! -x "$BOUNDARY_PY" ]; then
    err "缺少项目隔离 Python：$BOUNDARY_PY。请先运行 install_wsl_env.sh。"
    exit 1
fi
if [ -x "$BOUNDARY_PY" ]; then
    WS_PORT="$(
    QQ_WS_URL="$QQ_WS_URL" "$BOUNDARY_PY" - <<'PY'
import os
from urllib.parse import urlparse

url = urlparse(os.environ.get("QQ_WS_URL", "ws://127.0.0.1:3001"))
print(url.port or 3001)
PY
    )"
else
    WS_PORT="3001"
fi

if [ "$ACTION" = "bootstrap" ] || [ "$ACTION" = "sync-token" ] || [ "$ACTION" = "start" ] || [ "$ACTION" = "restart" ] || [ "$ACTION" = "status" ]; then
    if [ ! -r "$LOCAL_CONFIG" ]; then
        err "找不到 QQ 私有配置：$LOCAL_CONFIG"
        exit 1
    fi
    LOCAL_CONFIG_MODE="$(stat -c '%a' "$LOCAL_CONFIG" 2>/dev/null || printf '?')"
    if [ "$LOCAL_CONFIG_MODE" != "600" ]; then
        err "$LOCAL_CONFIG 权限必须为 0600（当前 $LOCAL_CONFIG_MODE）"
        exit 1
    fi
fi

validate_boundary_config() {
    PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        QQ_ACCESS_TOKEN="${QQ_ACCESS_TOKEN:-}" \
        "$BOUNDARY_PY" -m chatcopilot.platforms.qq.gateway_health \
        validate --url "$QQ_WS_URL" --url-env-key QQ_WS_URL \
        && PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        QQ_ACCESS_TOKEN="${QQ_ACCESS_TOKEN:-}" \
        "$BOUNDARY_PY" -m chatcopilot.platforms.qq.gateway_health \
        validate --url "${QQ_AT_PROXY_URL:-ws://127.0.0.1:3002}" \
        --url-env-key QQ_AT_PROXY_URL
}

validate_boundary_urls() {
    PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$BOUNDARY_PY" -m chatcopilot.platforms.qq.gateway_health \
        validate-url --url "$QQ_WS_URL" --url-env-key QQ_WS_URL \
        && PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$BOUNDARY_PY" -m chatcopilot.platforms.qq.gateway_health \
        validate-url --url "${QQ_AT_PROXY_URL:-ws://127.0.0.1:3002}" \
        --url-env-key QQ_AT_PROXY_URL
}

probe_boundary() {
    PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        QQ_ACCESS_TOKEN="${QQ_ACCESS_TOKEN:-}" \
        "$BOUNDARY_PY" -m chatcopilot.platforms.qq.gateway_health \
        probe --url "$QQ_WS_URL" --url-env-key QQ_WS_URL
}

run_external_check() {
    CHATCOPILOT_HOME="$REPO_ROOT" \
        PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$BOUNDARY_PY" -m chatcopilot bot external-check \
        --bot "$CHATCOPILOT_BOT_SPEC" \
        --config "$LOCAL_CONFIG"
}

probe_boundary_with_retry() {
    local attempt probe_output
    for attempt in $(seq 1 10); do
        if probe_output="$(probe_boundary 2>&1)"; then
            printf '%s\n' "$probe_output"
            return 0
        fi
        if [ "$attempt" -lt 10 ]; then
            sleep 1
        fi
    done
    printf '%s\n' "$probe_output" >&2
    return 1
}

if [ "$ACTION" = "bootstrap" ] || [ "$ACTION" = "sync-token" ]; then
    if ! validate_boundary_urls; then
        err "QQ OneBot URL 不满足回环边界；拒绝 bootstrap。"
        exit 1
    fi
elif [ "$ACTION" = "start" ] || [ "$ACTION" = "restart" ] || [ "$ACTION" = "status" ]; then
    if ! validate_boundary_config; then
        err "QQ OneBot 配置不满足安全边界；拒绝 $ACTION。"
        exit 1
    fi
fi

if ! command -v docker >/dev/null 2>&1; then
    err "未找到 docker。请先安装并启动 Docker，再重试。"
    exit 1
fi

container_exists() {
    docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER"
}

container_running() {
    docker ps --format '{{.Names}}' | grep -Fxq "$CONTAINER"
}

container_env_matches() {
    local env_lines
    env_lines="$(docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null)"
    printf '%s\n' "$env_lines" | grep -Fxq "NAPCAT_DISABLE_BYPASS=$NAPCAT_DISABLE_BYPASS" \
        && printf '%s\n' "$env_lines" | grep -Fxq "NAPCAT_DISABLE_MULTI_PROCESS=$NAPCAT_DISABLE_MULTI_PROCESS" \
        && printf '%s\n' "$env_lines" | grep -Fxq "ACCOUNT=$QQ_ACCOUNT" \
        && printf '%s\n' "$env_lines" | grep -Fxq "NODE_ENV=production"
}

container_image_matches() {
    [ "$(docker inspect "$CONTAINER" --format '{{.Config.Image}}' 2>/dev/null || true)" = "$NAPCAT_IMAGE" ]
}

container_shm_matches_expected() {
    local shm_bytes
    shm_bytes="$(docker inspect "$CONTAINER" --format '{{.HostConfig.ShmSize}}' 2>/dev/null || printf '0')"
    [[ "$shm_bytes" =~ ^[0-9]+$ ]] && [ "$shm_bytes" -eq "$NAPCAT_SHM_BYTES" ]
}

container_ports_are_loopback() {
    local bindings
    bindings="$(docker inspect "$CONTAINER" --format '{{json .HostConfig.PortBindings}}' 2>/dev/null)" \
        || return 1
    NAPCAT_PORT_BINDINGS="$bindings" NAPCAT_WS_PORT="$WS_PORT" \
        NAPCAT_WEBUI_PORT="$QQ_WEBUI_PORT" "$BOUNDARY_PY" - <<'PY'
import json
import os

bindings = json.loads(os.environ["NAPCAT_PORT_BINDINGS"])
expected = {
    "3001/tcp": os.environ["NAPCAT_WS_PORT"],
    "6099/tcp": os.environ["NAPCAT_WEBUI_PORT"],
}
for container_port, host_port in expected.items():
    entries = bindings.get(container_port)
    if not isinstance(entries, list) or len(entries) != 1:
        raise SystemExit(1)
    entry = entries[0]
    if not isinstance(entry, dict):
        raise SystemExit(1)
    if entry.get("HostIp") != "127.0.0.1" or entry.get("HostPort") != host_port:
        raise SystemExit(1)
PY
}

container_volume_name() {
    local destination="$1"
    docker inspect "$CONTAINER" \
        --format "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{.Name}}{{end}}{{end}}" \
        2>/dev/null
}

container_volumes_are_persistent() {
    local destination mount
    for destination in /app/.config/QQ /app/napcat/config; do
        mount="$(docker inspect "$CONTAINER" \
            --format "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{.Type}}:{{.Name}}{{end}}{{end}}" \
            2>/dev/null || true)"
        case "$mount" in
            volume:?*) ;;
            *) return 1 ;;
        esac
    done
}

container_restart_policy_matches() {
    [ "$(docker inspect "$CONTAINER" --format '{{.HostConfig.RestartPolicy.Name}}' 2>/dev/null || true)" = "unless-stopped" ]
}

container_needs_recreate() {
    container_image_matches \
        && container_env_matches \
        && container_shm_matches_expected \
        && container_ports_are_loopback \
        && container_volumes_are_persistent \
        && container_restart_policy_matches \
        && return 1
    return 0
}

create_container() {
    local qq_data_volume="$1"
    local napcat_config_volume="$2"
    local -a quick_pw_args=()
    if [ -n "${NAPCAT_QUICK_PASSWORD:-}" ]; then
        quick_pw_args+=(-e "NAPCAT_QUICK_PASSWORD=$NAPCAT_QUICK_PASSWORD")
    fi
    if [ -n "${NAPCAT_QUICK_PASSWORD_MD5:-}" ]; then
        quick_pw_args+=(-e "NAPCAT_QUICK_PASSWORD_MD5=$NAPCAT_QUICK_PASSWORD_MD5")
    fi
    if ! docker run -d --name "$CONTAINER" \
        -e ACCOUNT="$QQ_ACCOUNT" \
        -e NODE_ENV=production \
        -e "NAPCAT_DISABLE_BYPASS=$NAPCAT_DISABLE_BYPASS" \
        -e "NAPCAT_DISABLE_MULTI_PROCESS=$NAPCAT_DISABLE_MULTI_PROCESS" \
        "${quick_pw_args[@]}" \
        --shm-size "$NAPCAT_SHM_SIZE" \
        -v "$qq_data_volume:/app/.config/QQ" \
        -v "$napcat_config_volume:/app/napcat/config" \
        -p "127.0.0.1:$WS_PORT:3001" \
        -p "127.0.0.1:$QQ_WEBUI_PORT:6099" \
        --restart unless-stopped \
        "$NAPCAT_IMAGE" >/dev/null; then
        err "创建 NapCat 容器失败：$CONTAINER"
        return 1
    fi
}

recreate_container() {
    local qq_data_volume napcat_config_volume
    qq_data_volume="$(container_volume_name "/app/.config/QQ")"
    napcat_config_volume="$(container_volume_name "/app/napcat/config")"
    qq_data_volume="${qq_data_volume:-$NAPCAT_QQ_DATA_VOLUME}"
    napcat_config_volume="${napcat_config_volume:-$NAPCAT_CONFIG_VOLUME}"

    warn "检测到旧 NapCat 容器的账户、镜像、端口、volume 或重启策略不符合当前配置。"
    warn "会保留 Docker volume：QQ 数据=$qq_data_volume，NapCat 配置=$napcat_config_volume。"
    if [ ! -t 0 ] || [ ! -t 1 ]; then
        err "重建容器需要可信交互式终端确认。"
        return 1
    fi
    printf '确认停止并重建容器 %s？ [y/N] ' "$CONTAINER" > /dev/tty
    local reply
    IFS= read -r reply < /dev/tty || return 1
    if [[ ! "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        err "用户未确认重建 NapCat 容器。"
        return 1
    fi
    if ! docker image inspect "$NAPCAT_IMAGE" >/dev/null 2>&1 \
        && ! docker pull "$NAPCAT_IMAGE"; then
        err "拉取固定 NapCat 镜像失败；旧容器保持运行。"
        return 1
    fi
    if container_running && ! docker stop "$CONTAINER" >/dev/null; then
        err "停止旧 NapCat 容器失败：$CONTAINER"
        return 1
    fi
    if ! docker rm "$CONTAINER" >/dev/null; then
        err "删除旧 NapCat 容器失败：$CONTAINER"
        return 1
    fi
    if ! create_container "$qq_data_volume" "$napcat_config_volume"; then
        return 1
    fi
    ok "NapCat 容器已重建并启动：$CONTAINER"
}

ensure_bootstrap_container() {
    if container_exists; then
        if container_needs_recreate; then
            if ! recreate_container; then
                return 1
            fi
        else
            if ! docker start "$CONTAINER" >/dev/null; then
                err "启动 NapCat 容器失败：$CONTAINER"
                return 1
            fi
            ok "NapCat 回环容器已启动：$CONTAINER"
        fi
    else
        if ! create_container "$NAPCAT_QQ_DATA_VOLUME" "$NAPCAT_CONFIG_VOLUME"; then
            return 1
        fi
        ok "NapCat 回环容器已创建并启动：$CONTAINER"
    fi
}

token_is_valid() {
    printf '%s' "${QQ_ACCESS_TOKEN:-}" \
        | PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
            "$BOUNDARY_PY" -m chatcopilot.platforms.qq.token_sync validate \
            >/dev/null 2>&1
}

generate_access_token() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
        return
    fi
    PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$BOUNDARY_PY" -c \
        'from chatcopilot.platforms.qq.token_sync import generate_access_token; print(generate_access_token())'
}

sync_local_env_token() {
    printf '%s' "$QQ_ACCESS_TOKEN" \
        | PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
            "$BOUNDARY_PY" -m chatcopilot.botspec.qq_token_sync \
            --path "$LOCAL_CONFIG" --bot "$CHATCOPILOT_BOT_SPEC" \
            --bots-root "$REPO_ROOT/bots"
}

provision_runtime_env() {
    CHATCOPILOT_HOME="$REPO_ROOT" \
        PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$BOUNDARY_PY" -m chatcopilot bot provision-env \
        --bot "$REPO_ROOT/bots/$INSTANCE/bot.yaml" \
        --config "$LOCAL_CONFIG"
}

sync_container_onebot_token() {
    local napcat_config_volume python_script
    napcat_config_volume="$(container_volume_name "/app/napcat/config")"
    if [ -z "$napcat_config_volume" ]; then
        err "无法解析 NapCat config volume；拒绝写入 OneBot token。"
        return 1
    fi
    python_script='
import json
import os
from pathlib import Path
import re
import sys

token = sys.stdin.read().strip()
if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None:
    raise SystemExit("invalid OneBot token received on stdin")
account = os.environ.get("ACCOUNT", "").strip()
if not account:
    raise SystemExit("ACCOUNT is missing")
path = Path(f"/app/napcat/config/onebot11_{account}.json")
config = json.loads(path.read_text(encoding="utf-8"))
network = config.setdefault("network", {})
if not isinstance(network, dict):
    raise SystemExit("network must be an object")
servers = network.setdefault("websocketServers", [])
if not isinstance(servers, list):
    raise SystemExit("websocketServers must be an array")
targets = [
    server
    for server in servers
    if isinstance(server, dict) and int(server.get("port", 0)) == 3001
]
if not targets:
    server = {
        "name": "agentstrata-websocket-server",
        "enable": True,
        "host": "0.0.0.0",
        "port": 3001,
        "messagePostFormat": "array",
        "reportSelfMessage": False,
        "token": token,
        "enableForcePushEvent": True,
        "debug": False,
        "heartInterval": 30000,
    }
    servers.append(server)
    targets = [server]
for server in targets:
    server["enable"] = True
    server["host"] = "0.0.0.0"
    server["token"] = token
tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.chmod(path.stat().st_mode & 0o777)
os.replace(tmp, path)
print(json.dumps({"updated": len(targets), "tokenLength": len(token)}))
'

    if ! docker stop "$CONTAINER" >/dev/null; then
        err "停止 NapCat 容器以同步 OneBot token 失败：$CONTAINER"
        return 1
    fi
    if ! printf '%s' "$QQ_ACCESS_TOKEN" \
        | docker run --rm -i \
            --entrypoint python3 \
            -e "ACCOUNT=$QQ_ACCOUNT" \
            -v "$napcat_config_volume:/app/napcat/config" \
            "$NAPCAT_IMAGE" \
            -c "$python_script"; then
        err "写入 NapCat OneBot token 失败；正在恢复容器运行。"
        docker start "$CONTAINER" >/dev/null 2>&1 || true
        return 1
    fi
    if ! docker start "$CONTAINER" >/dev/null; then
        err "OneBot token 已写入，但恢复 NapCat 容器运行失败：$CONTAINER"
        return 1
    fi
}

sync_gateway_token() {
    local token_source
    if token_is_valid; then
        token_source="reused"
    else
        QQ_ACCESS_TOKEN="$(generate_access_token)"
        token_source="generated"
    fi
    if ! sync_local_env_token; then
        err "更新 bot-owned local.env 失败。"
        return 1
    fi
    if ! provision_runtime_env; then
        err "生成实例运行时 env 失败。"
        return 1
    fi
    if ! sync_container_onebot_token; then
        return 1
    fi
    if ! probe_boundary_with_retry; then
        err "OneBot token 已同步，但双向认证探针失败。"
        return 1
    fi
    ok "OneBot token 已同步并通过双向认证（source=$token_source, length=${#QQ_ACCESS_TOKEN}）。"
}

case "$ACTION" in
    bootstrap)
        if ! ensure_bootstrap_container; then
            exit 1
        fi
        warn "bootstrap 仅提供 localhost WebUI，不验证 token，也不启动 QQ Bot service。"
        info "请只完成本地 WebUI 扫码；后续 sync-token 会创建并保护正向 WebSocket :3001。"
        ;;
    sync-token)
        if ! ensure_bootstrap_container; then
            exit 1
        fi
        if ! sync_gateway_token; then
            exit 1
        fi
        ;;
    status)
        status_rc=0
        info "container: $CONTAINER"
        if container_exists; then
            docker ps -a --filter "name=^/${CONTAINER}$"
            info "Crash guard: NAPCAT_DISABLE_BYPASS=$(
                docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
                    | grep '^NAPCAT_DISABLE_BYPASS=' 2>/dev/null \
                    | sed 's/^NAPCAT_DISABLE_BYPASS=//' || true
            ), NAPCAT_DISABLE_MULTI_PROCESS=$(
                docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
                    | grep '^NAPCAT_DISABLE_MULTI_PROCESS=' 2>/dev/null \
                    | sed 's/^NAPCAT_DISABLE_MULTI_PROCESS=//' || true
            ), shm=$(
                docker inspect "$CONTAINER" --format '{{.HostConfig.ShmSize}}' 2>/dev/null || printf '?'
            ) bytes"
            if container_needs_recreate; then
                warn "容器镜像、crash guard 或回环端口绑定不符合当前配置；运行 start 会自动重建。"
                status_rc=1
            fi
            if container_running; then
                if ! run_external_check; then
                    err "QQ 外部平台检查失败。"
                    status_rc=1
                fi
            else
                warn "容器未运行，无法执行 OneBot 双向认证探针。"
                status_rc=1
            fi
        else
            warn "容器不存在"
            status_rc=1
        fi
        info "OneBot WS: $QQ_WS_URL"
        info "NapCat WebUI: http://localhost:$QQ_WEBUI_PORT"
        exit "$status_rc"
        ;;
    logs)
        if ! container_exists; then
            err "容器不存在：$CONTAINER"
            exit 1
        fi
        docker logs -f "$CONTAINER"
        ;;
    start)
        if container_exists; then
            if container_needs_recreate; then
                if ! recreate_container; then
                    exit 1
                fi
            else
                if ! docker start "$CONTAINER" >/dev/null; then
                    err "启动 NapCat 容器失败：$CONTAINER"
                    exit 1
                fi
                ok "NapCat 容器已启动：$CONTAINER"
            fi
        else
            if ! create_container "$NAPCAT_QQ_DATA_VOLUME" "$NAPCAT_CONFIG_VOLUME"; then
                exit 1
            fi
            ok "NapCat 容器已创建并启动：$CONTAINER"
        fi
        info "Crash guard: NAPCAT_DISABLE_BYPASS=$NAPCAT_DISABLE_BYPASS, NAPCAT_DISABLE_MULTI_PROCESS=$NAPCAT_DISABLE_MULTI_PROCESS, shm=$NAPCAT_SHM_SIZE"
        info "首次登录：运行 docker logs -f $CONTAINER，用手机 QQ 扫码。"
        info "WebUI: http://localhost:$QQ_WEBUI_PORT"
        info "如需重新配置正向 WebSocket :3001，请运行 sync-token。"
        if ! probe_boundary_with_retry; then
            err "QQ OneBot 双向认证探针失败；容器保留运行以便通过 localhost WebUI 修正配置，但 gateway start 失败。"
            exit 1
        fi
        ok "QQ OneBot 无 token 拒绝、带 token 接受。"
        ;;
    restart)
        if container_exists; then
            if container_needs_recreate; then
                if ! recreate_container; then
                    exit 1
                fi
            else
                if ! docker restart "$CONTAINER" >/dev/null; then
                    err "重启 NapCat 容器失败：$CONTAINER"
                    exit 1
                fi
                ok "NapCat 容器已重启：$CONTAINER"
            fi
        else
            if ! create_container "$NAPCAT_QQ_DATA_VOLUME" "$NAPCAT_CONFIG_VOLUME"; then
                exit 1
            fi
            ok "NapCat 容器已创建并启动：$CONTAINER"
        fi
        info "Crash guard: NAPCAT_DISABLE_BYPASS=$NAPCAT_DISABLE_BYPASS, NAPCAT_DISABLE_MULTI_PROCESS=$NAPCAT_DISABLE_MULTI_PROCESS, shm=$NAPCAT_SHM_SIZE"
        if ! probe_boundary_with_retry; then
            err "QQ OneBot 双向认证探针失败；容器保留运行以便通过 localhost WebUI 修正配置，但 gateway restart 失败。"
            exit 1
        fi
        ok "QQ OneBot 无 token 拒绝、带 token 接受。"
        ;;
esac
