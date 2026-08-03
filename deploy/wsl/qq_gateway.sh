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
if [ -r "$LOCAL_CONFIG" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$LOCAL_CONFIG"
    set +a
fi

CONTAINER="napcat-$INSTANCE"
QQ_WS_URL="${QQ_WS_URL:-ws://127.0.0.1:3001}"
QQ_WEBUI_PORT="${QQ_WEBUI_PORT:-6099}"
NAPCAT_DISABLE_BYPASS="${NAPCAT_DISABLE_BYPASS:-1}"
NAPCAT_DISABLE_MULTI_PROCESS="${NAPCAT_DISABLE_MULTI_PROCESS:-1}"
NAPCAT_SHM_SIZE="${NAPCAT_SHM_SIZE:-512m}"
NAPCAT_QQ_DATA_VOLUME="${NAPCAT_QQ_DATA_VOLUME:-napcat-${INSTANCE}-qq-data}"
NAPCAT_CONFIG_VOLUME="${NAPCAT_CONFIG_VOLUME:-napcat-${INSTANCE}-config}"

WS_PORT="$(
    QQ_WS_URL="$QQ_WS_URL" python3 - <<'PY'
import os
from urllib.parse import urlparse

url = urlparse(os.environ.get("QQ_WS_URL", "ws://127.0.0.1:3001"))
print(url.port or 3001)
PY
)"

if { [ "$ACTION" = "bootstrap" ] || [ "$ACTION" = "sync-token" ] || [ "$ACTION" = "start" ] || [ "$ACTION" = "restart" ]; } && [ -z "${QQ_ACCOUNT:-}" ]; then
    err "缺少 QQ_ACCOUNT。请先填写 bots/$INSTANCE/local.env，并运行：bash deploy/wsl/update_instance.sh --instance $INSTANCE"
    exit 1
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

BOUNDARY_PY=""
if [ "$ACTION" = "bootstrap" ] || [ "$ACTION" = "sync-token" ] || [ "$ACTION" = "start" ] || [ "$ACTION" = "restart" ] || [ "$ACTION" = "status" ]; then
    BOUNDARY_PY="$REPO_ROOT/.venv/bin/python"
    if [ ! -x "$BOUNDARY_PY" ]; then
        BOUNDARY_PY="$(command -v python3 || command -v python || true)"
    fi
    if [ -z "${BOUNDARY_PY:-}" ]; then
        err "未找到 Python，无法校验 QQ OneBot 安全边界。"
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
        && printf '%s\n' "$env_lines" | grep -Fxq "NAPCAT_DISABLE_MULTI_PROCESS=$NAPCAT_DISABLE_MULTI_PROCESS"
}

container_shm_is_sane() {
    local shm_bytes
    shm_bytes="$(docker inspect "$CONTAINER" --format '{{.HostConfig.ShmSize}}' 2>/dev/null || printf '0')"
    [ "${shm_bytes:-0}" -ge 268435456 ]
}

container_ports_are_loopback() {
    local ws_host webui_host
    ws_host="$(docker inspect "$CONTAINER" \
        --format '{{(index (index .HostConfig.PortBindings "3001/tcp") 0).HostIp}}' \
        2>/dev/null || true)"
    webui_host="$(docker inspect "$CONTAINER" \
        --format '{{(index (index .HostConfig.PortBindings "6099/tcp") 0).HostIp}}' \
        2>/dev/null || true)"
    [ "$ws_host" = "127.0.0.1" ] && [ "$webui_host" = "127.0.0.1" ]
}

container_volume_name() {
    local destination="$1"
    docker inspect "$CONTAINER" \
        --format "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{.Name}}{{end}}{{end}}" \
        2>/dev/null
}

container_needs_recreate() {
    container_env_matches && container_shm_is_sane && container_ports_are_loopback && return 1
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
    docker run -d --name "$CONTAINER" \
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
        mlikiowa/napcat-docker:latest >/dev/null
}

recreate_container() {
    local qq_data_volume napcat_config_volume
    qq_data_volume="$(container_volume_name "/app/.config/QQ")"
    napcat_config_volume="$(container_volume_name "/app/napcat/config")"
    qq_data_volume="${qq_data_volume:-$NAPCAT_QQ_DATA_VOLUME}"
    napcat_config_volume="${napcat_config_volume:-$NAPCAT_CONFIG_VOLUME}"

    warn "检测到旧 NapCat 容器不满足 crash guard 或回环端口绑定要求，将重建容器。"
    warn "会保留 Docker volume：QQ 数据=$qq_data_volume，NapCat 配置=$napcat_config_volume。"
    docker stop "$CONTAINER" >/dev/null 2>&1 || true
    docker rm "$CONTAINER" >/dev/null
    create_container "$qq_data_volume" "$napcat_config_volume"
    ok "NapCat 容器已重建并启动：$CONTAINER"
}

ensure_bootstrap_container() {
    if container_exists; then
        if container_needs_recreate; then
            recreate_container
        else
            docker start "$CONTAINER" >/dev/null
            ok "NapCat 回环容器已启动：$CONTAINER"
        fi
    else
        create_container "$NAPCAT_QQ_DATA_VOLUME" "$NAPCAT_CONFIG_VOLUME"
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
            "$BOUNDARY_PY" -m chatcopilot.platforms.qq.token_sync \
            sync-local-env --path "$LOCAL_CONFIG"
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
servers = config.get("network", {}).get("websocketServers")
if not isinstance(servers, list):
    raise SystemExit("websocketServers is missing")
targets = [server for server in servers if int(server.get("port", 0)) == 3001]
if not targets:
    raise SystemExit("OneBot websocket server :3001 is missing")
for server in targets:
    server["enable"] = True
    server["token"] = token
tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.chmod(path.stat().st_mode & 0o777)
os.replace(tmp, path)
print(json.dumps({"updated": len(targets), "tokenLength": len(token)}))
'

    docker stop "$CONTAINER" >/dev/null
    if ! printf '%s' "$QQ_ACCESS_TOKEN" \
        | docker run --rm -i \
            --entrypoint python3 \
            -e "ACCOUNT=$QQ_ACCOUNT" \
            -v "$napcat_config_volume:/app/napcat/config" \
            mlikiowa/napcat-docker:latest \
            -c "$python_script"; then
        err "写入 NapCat OneBot token 失败；正在恢复容器运行。"
        docker start "$CONTAINER" >/dev/null 2>&1 || true
        return 1
    fi
    docker start "$CONTAINER" >/dev/null
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
        ensure_bootstrap_container
        warn "bootstrap 仅提供 localhost WebUI，不验证 token，也不启动 QQ Bot service。"
        info "请在 http://localhost:$QQ_WEBUI_PORT 配置正向 WebSocket :3001 与强 Access Token。"
        ;;
    sync-token)
        ensure_bootstrap_container
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
                warn "容器不满足 crash guard 或回环端口绑定要求；运行 start 会自动重建。"
                status_rc=1
            fi
            if container_running; then
                if ! probe_boundary; then
                    err "QQ OneBot 双向认证探针失败。"
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
                recreate_container
            else
                docker start "$CONTAINER" >/dev/null
                ok "NapCat 容器已启动：$CONTAINER"
            fi
        else
            create_container "$NAPCAT_QQ_DATA_VOLUME" "$NAPCAT_CONFIG_VOLUME"
            ok "NapCat 容器已创建并启动：$CONTAINER"
        fi
        info "Crash guard: NAPCAT_DISABLE_BYPASS=$NAPCAT_DISABLE_BYPASS, NAPCAT_DISABLE_MULTI_PROCESS=$NAPCAT_DISABLE_MULTI_PROCESS, shm=$NAPCAT_SHM_SIZE"
        info "首次登录：运行 docker logs -f $CONTAINER，用手机 QQ 扫码。"
        info "WebUI: http://localhost:$QQ_WEBUI_PORT"
        info "请在 WebUI 启用正向 WebSocket，端口 3001；Access Token 与 QQ_ACCESS_TOKEN 保持一致。"
        if ! probe_boundary_with_retry; then
            err "QQ OneBot 双向认证探针失败；容器保留运行以便通过 localhost WebUI 修正配置，但 gateway start 失败。"
            exit 1
        fi
        ok "QQ OneBot 无 token 拒绝、带 token 接受。"
        ;;
    restart)
        if container_exists; then
            if container_needs_recreate; then
                recreate_container
            else
                docker restart "$CONTAINER" >/dev/null
                ok "NapCat 容器已重启：$CONTAINER"
            fi
        else
            create_container "$NAPCAT_QQ_DATA_VOLUME" "$NAPCAT_CONFIG_VOLUME"
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
