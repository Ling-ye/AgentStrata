#!/usr/bin/env bash
# Manage shared Docker infrastructure services.
#
# Usage:
#   bash deploy/docker/services.sh start
#   bash deploy/docker/services.sh stop
#   bash deploy/docker/services.sh status
#   bash deploy/docker/services.sh logs [service]
#   bash deploy/docker/services.sh doctor all
#   bash deploy/docker/services.sh desired
#   bash deploy/docker/services.sh doctor searxng
#   bash deploy/docker/services.sh doctor xhs
#   bash deploy/docker/services.sh probe xhs --keyword "青山制面 上海"
#   bash deploy/docker/services.sh probe playwright
#   bash deploy/docker/services.sh login xhs --qrcode
#   bash deploy/docker/services.sh login xhs --qrcode --output /tmp/xhs.png
#   bash deploy/docker/services.sh login xhs --qrcode --print-data-url
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
ACTION="${1:-}"
[ $# -gt 0 ] && shift || true

XHS_CONTAINER="chatcopilot-xiaohongshu-mcp"
SEARXNG_CONTAINER="chatcopilot-searxng"
PLAYWRIGHT_CONTAINER="chatcopilot-playwright-mcp"

XHS_PORT="18060"
XHS_URL="http://localhost:${XHS_PORT}/mcp"
SEARXNG_HTTP_PORT="18064"
SEARXNG_URL="http://127.0.0.1:${SEARXNG_HTTP_PORT}"
PLAYWRIGHT_PORT="18066"
PLAYWRIGHT_URL="http://localhost:${PLAYWRIGHT_PORT}/mcp"

RETAINED_SERVICES=(searxng playwright-mcp xiaohongshu-mcp)
DESIRED_SERVICES=()

ok()   { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
info() { printf "\033[1;36m[*]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ERR]\033[0m %s\n" "$*" >&2; }

compose() {
    local env_args=()
    if [ -f "$SCRIPT_DIR/.env" ]; then
        env_args=(--env-file "$SCRIPT_DIR/.env")
    fi
    docker compose "${env_args[@]}" -f "$SCRIPT_DIR/docker-compose.yaml" --profile "*" "$@"
}

reject_port_overrides() {
    local name
    for name in XHS_MCP_PORT SEARXNG_PORT PLAYWRIGHT_MCP_PORT; do
        if [[ -v "$name" ]]; then
            err "$name is no longer configurable; shared-service ports are fixed by the reviewed runtime contract."
            return 1
        fi
    done
    if [ -f "$SCRIPT_DIR/.env" ] && grep -Eq \
        '^[[:space:]]*(export[[:space:]]+)?(XHS_MCP_PORT|SEARXNG_PORT|PLAYWRIGHT_MCP_PORT)[[:space:]]*=' \
        "$SCRIPT_DIR/.env"; then
        err "Docker port override keys are unsupported in deploy/docker/.env; remove them before continuing."
        return 1
    fi
    return 0
}

select_python() {
    local repo_python="$REPO_ROOT/.venv/bin/python"
    if [ -x "$repo_python" ]; then
        printf '%s\n' "$repo_python"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1 && python3 -c 'import ruamel.yaml' >/dev/null 2>&1; then
        command -v python3
        return 0
    fi
    err "Cannot resolve Docker desired state: repo .venv is missing and python3 cannot import ruamel.yaml."
    return 1
}

load_desired_services() {
    local python_bin output service
    python_bin="$(select_python)" || return 1
    if ! output="$("$python_bin" "$SCRIPT_DIR/desired_state.py" --repo-root "$REPO_ROOT")"; then
        err "Docker desired-state resolution failed; no containers were changed."
        return 1
    fi
    DESIRED_SERVICES=()
    while IFS= read -r service; do
        [ -n "$service" ] && DESIRED_SERVICES+=("$service")
    done <<< "$output"
    return 0
}

is_desired() {
    local wanted="$1" service
    for service in "${DESIRED_SERVICES[@]}"; do
        [ "$service" = "$wanted" ] && return 0
    done
    return 1
}

print_desired_services() {
    load_desired_services || return 1
    if [ "${#DESIRED_SERVICES[@]}" -eq 0 ]; then
        info "No Docker shared services are enabled by the discovered BotSpecs."
        return 0
    fi
    printf '%s\n' "${DESIRED_SERVICES[@]}"
}

reconcile_services() {
    local service
    load_desired_services || return 1
    if [ "${#DESIRED_SERVICES[@]}" -eq 0 ]; then
        info "Desired state is empty; stopping the Compose project."
        compose down --remove-orphans
        return $?
    fi

    info "Starting desired services: ${DESIRED_SERVICES[*]}"
    if ! compose up -d "${DESIRED_SERVICES[@]}"; then
        err "Failed to start desired Docker services; existing optional services were not stopped."
        return 1
    fi
    for service in "${RETAINED_SERVICES[@]}"; do
        if ! is_desired "$service"; then
            info "Stopping disabled service: $service"
            compose stop "$service" || return 1
        fi
    done
    # Retire containers removed from the reviewed Compose file only after all
    # desired services started successfully.
    compose up -d --no-recreate --remove-orphans "${DESIRED_SERVICES[@]}"
}

require_container_running() {
    local container="$1"
    if ! docker ps --format '{{.Names}}' | grep -Fxq "$container"; then
        err "Container $container is not running. Start it explicitly or reconcile desired state."
        return 1
    fi
}

require_xhs_running() { require_container_running "$XHS_CONTAINER"; }

json_string() {
    python3 -c 'import json,sys; print(json.dumps(sys.argv[1], ensure_ascii=False))' "$1"
}

mcp_session_call() {
    local url="$1"
    local tool_name="$2"
    local args_json="${3:-}"
    local timeout_s="${4:-120}"
    local hdr_file payload_json
    if [ -z "$args_json" ]; then
        args_json="{}"
    fi
    hdr_file="$(mktemp)"

    if ! curl -sfS --max-time "$timeout_s" -D "$hdr_file" -o /dev/null -X POST "$url" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"chatcopilot-services","version":"1.0"}}}'; then
        rm -f "$hdr_file"
        return 1
    fi

    local sid
    sid="$(grep -i '^mcp-session-id:' "$hdr_file" | tr -d '\r' | awk '{print $2}')"
    rm -f "$hdr_file"

    local sid_args=()
    if [ -n "$sid" ]; then
        sid_args=(-H "Mcp-Session-Id: $sid")
    fi

    if ! curl -sfS --max-time "$timeout_s" -o /dev/null -X POST "$url" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        "${sid_args[@]}" \
        -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'; then
        return 1
    fi

    if ! payload_json="$(python3 -c '
import json, sys
try:
    arguments = json.loads(sys.argv[2] or "{}")
except json.JSONDecodeError as exc:
    raise SystemExit(f"invalid MCP arguments JSON: {exc}: {sys.argv[2]!r}")
payload = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": sys.argv[1], "arguments": arguments},
}
print(json.dumps(payload, ensure_ascii=False))
' "$tool_name" "$args_json")"; then
        return 1
    fi

    printf '%s' "$payload_json" |
    curl -sfS --max-time "$timeout_s" -X POST "$url" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        "${sid_args[@]}" \
        --data-binary @-
}

xhs_mcp_call() {
    mcp_session_call "$XHS_URL" "$1" "${2:-}" "${3:-120}"
}

extract_qrcode_png() {
    local output_path="$1"
    local raw_file rc
    raw_file="$(mktemp)"
    cat > "$raw_file"
    python3 - "$output_path" "$raw_file" <<'PY'
import base64
import json
import re
import sys

out = sys.argv[1]
raw_path = sys.argv[2]
with open(raw_path, "r", encoding="utf-8", errors="replace") as f:
    raw = f.read()
objects = []

for line in raw.splitlines():
    line = line.strip()
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line or not line.startswith("{"):
        continue
    try:
        objects.append(json.loads(line))
    except ValueError:
        pass

if not objects:
    try:
        objects.append(json.loads(raw))
    except ValueError:
        pass

text_parts = []
image_data = None
for obj in objects:
    result = obj.get("result") if isinstance(obj, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text") or ""))
            if isinstance(item, dict) and item.get("type") == "image":
                image_data = str(item.get("data") or "")
    text_parts.append(json.dumps(obj, ensure_ascii=False))

blob = "\n".join(text_parts)
if image_data:
    data = image_data
else:
    match = re.search(r"data:image/(?:png|jpeg);base64,([A-Za-z0-9+/=\n\r]+)", blob)
    if not match:
        match = re.search(r'"img"\s*:\s*"([^"]+)"', blob)
    if not match:
        print(raw)
        sys.exit(2)
    data = match.group(1)
data = data.replace("\\n", "").replace("\n", "").replace("\r", "")
if data.startswith("data:image"):
    data = data.split(",", 1)[1]
with open(out, "wb") as f:
    f.write(base64.b64decode(data))
print(out)
PY
    rc=$?
    rm -f "$raw_file"
    return "$rc"
}

transport_doctor() {
    local container="$1"
    local compose_svc="$2"
    local url="$3"
    local expected_statuses="$4"
    local rc=0

    info "[process] Checking Docker container $container..."
    if docker ps --format '{{.Names}}' | grep -Fxq "$container"; then
        ok "$container is running."
    else
        err "$container is not running."
        rc=1
    fi

    info "[process] Checking Compose status..."
    compose ps "$compose_svc" || rc=1

    info "[transport] Checking HTTP endpoint $url..."
    if curl -sS --max-time 5 -o /dev/null -w "%{http_code}\n" "$url" | grep -Eq "$expected_statuses"; then
        ok "HTTP transport responds."
    else
        warn "HTTP transport did not respond with an expected status."
        rc=1
    fi

    if docker ps --format '{{.Names}}' | grep -Fxq "$container"; then
        info "Recent errors from container logs..."
        docker logs --tail 100 "$container" 2>&1 |
            grep -Ei "error|panic|timeout|ERR_|crash" |
            tail -30 || true
    fi

    return "$rc"
}

xhs_doctor() {
    local rc=0
    info "[process] Checking Docker container..."
    if require_xhs_running; then
        ok "$XHS_CONTAINER is running."
    else
        rc=1
    fi

    info "[process] Checking Compose status..."
    compose ps xiaohongshu-mcp || rc=1

    info "[transport] Checking HTTP endpoint $XHS_URL..."
    if curl -sS --max-time 5 -o /dev/null -w "%{http_code}\n" "$XHS_URL" | grep -Eq '^(200|202|400|405)$'; then
        ok "MCP transport responds."
    else
        warn "MCP endpoint did not respond with an expected status."
        rc=1
    fi

    if docker ps --format '{{.Names}}' | grep -Fxq "$XHS_CONTAINER"; then
        info "[transport] Checking container DNS..."
        docker exec "$XHS_CONTAINER" sh -lc 'getent hosts www.xiaohongshu.com xiaohongshu.com >/tmp/xhs_dns.out 2>/tmp/xhs_dns.err; cat /tmp/xhs_dns.out; cat /tmp/xhs_dns.err >&2'

        info "[credential/login] Checking non-secret environment paths..."
        docker exec "$XHS_CONTAINER" sh -lc '
            printenv | sort | grep -E "^(COOKIES_PATH|HOME|XDG_CACHE_HOME|XDG_CONFIG_HOME)=" || true
            if [ -n "${XHS_PROXY:-}" ]; then
                echo "XHS_PROXY=configured"
            else
                echo "XHS_PROXY=not_configured"
            fi
        '

        info "[credential/login] Checking cookie and browser profile files..."
        docker exec "$XHS_CONTAINER" sh -lc '
            for path in "${COOKIES_PATH:-/app/data/cookies.json}" /app/data/home /app/data/cache /app/data/config; do
                if [ -e "$path" ]; then
                    ls -ld "$path"
                else
                    echo "missing: $path"
                fi
            done
        '

        info "[process] Recent errors from container logs..."
        docker logs --tail 200 "$XHS_CONTAINER" 2>&1 |
            grep -Ei "error|panic|timeout|ERR_|not resolved|登录|cookie|search|搜索" |
            tail -60 || true
    fi

    if [ "$rc" -eq 0 ]; then
        info "[credential/login] Calling check_login_status..."
        if xhs_mcp_call check_login_status '{}' 30; then
            ok "Xiaohongshu login-status tool responded."
        else
            warn "Xiaohongshu login-status tool failed."
            rc=1
        fi
    fi

    return "$rc"
}

xhs_probe() {
    local keyword="青山制面 上海"
    while [ $# -gt 0 ]; do
        case "$1" in
            --keyword)
                keyword="${2:-$keyword}"
                shift 2
                ;;
            *)
                err "Unknown probe option: $1"
                return 2
                ;;
        esac
    done

    require_xhs_running || return 1

    info "Checking login status..."
    local status
    if ! status="$(xhs_mcp_call check_login_status '{}' 30 2>&1)"; then
        err "check_login_status failed:"
        echo "$status"
        return 1
    fi
    echo "$status"

    info "Searching Xiaohongshu with keyword: $keyword"
    local kw_json args result
    kw_json="$(json_string "$keyword")"
    args="{\"keyword\":${kw_json}}"
    if ! result="$(xhs_mcp_call search_feeds "$args" 120 2>&1)"; then
        err "search_feeds failed:"
        echo "$result"
        return 1
    fi
    echo "$result"
}

searxng_probe() {
    local keyword="AgentStrata"
    while [ $# -gt 0 ]; do
        case "$1" in
            --keyword)
                keyword="${2:-$keyword}"
                shift 2
                ;;
            *)
                err "Unknown probe option: $1"
                return 2
                ;;
        esac
    done

    require_container_running "$SEARXNG_CONTAINER" || return 1
    info "[functional] Querying the SearXNG JSON API: $keyword"
    python3 - "$SEARXNG_URL" "$keyword" <<'PY'
import json
import sys
import urllib.parse
import urllib.request

base_url, keyword = sys.argv[1].rstrip("/"), sys.argv[2]
query = urllib.parse.urlencode(
    {"q": keyword, "format": "json", "categories": "general", "safesearch": "1"}
)
request = urllib.request.Request(
    f"{base_url}/search?{query}",
    headers={"Accept": "application/json", "User-Agent": "AgentStrata-Docker-Doctor/1.0"},
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
except Exception as exc:
    print(f"SearXNG functional probe failed: {exc}", file=sys.stderr)
    raise SystemExit(1)
if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
    print("SearXNG response is missing a results list", file=sys.stderr)
    raise SystemExit(1)
print(json.dumps(
    {
        "ok": True,
        "query": payload.get("query"),
        "result_count": len(payload["results"]),
        "results": [
            {"title": item.get("title"), "url": item.get("url")}
            for item in payload["results"][:3]
            if isinstance(item, dict)
        ],
    },
    ensure_ascii=False,
    indent=2,
))
PY
}

playwright_probe() {
    local result
    require_container_running "$PLAYWRIGHT_CONTAINER" || return 1
    info "[functional] Launching Chromium through Playwright MCP..."
    if ! result="$(mcp_session_call "$PLAYWRIGHT_URL" browser_navigate '{"url":"about:blank"}' 30 2>&1)"; then
        err "Playwright browser_navigate failed:"
        echo "$result"
        return 1
    fi
    if printf '%s' "$result" | grep -Eq '"isError"[[:space:]]*:[[:space:]]*true'; then
        err "Playwright returned an MCP tool error:"
        echo "$result"
        return 1
    fi
    ok "Playwright launched Chromium and completed browser_navigate."
}

xhs_login() {
    local qrcode=0
    local output=""
    local print_data_url=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --qrcode)
                qrcode=1
                shift
                ;;
            --output)
                output="${2:-}"
                shift 2
                ;;
            --print-data-url)
                print_data_url=1
                qrcode=1
                shift
                ;;
            *)
                err "Unknown login option: $1"
                return 2
                ;;
        esac
    done

    require_xhs_running || return 1

    info "Checking login status via MCP ($XHS_URL)..."
    xhs_mcp_call check_login_status '{}' 30 || true
    echo

    if [ "$qrcode" -eq 1 ]; then
        local png="$output"
        if [ -z "$png" ]; then
            png="$(mktemp --suffix=.png 2>/dev/null || mktemp)"
        fi
        info "Requesting login QR code..."
        if xhs_mcp_call get_login_qrcode '{}' 60 | extract_qrcode_png "$png"; then
            if [ "$print_data_url" -eq 1 ]; then
                printf 'data:image/png;base64,'
                base64 -w 0 "$png"
                printf '\n'
            else
                ok "QR code written to $png"
            fi
        else
            err "Could not extract QR code image from MCP response."
            return 1
        fi
    else
        info "If not logged in, run:"
        echo "  bash deploy/docker/services.sh login xhs --qrcode"
    fi
}

case "$ACTION" in
    start|stop|status|logs|doctor|probe|login)
        reject_port_overrides || exit 2
        ;;
esac

case "$ACTION" in
    start)
        if [ "$#" -eq 0 ]; then
            if ! reconcile_services; then
                exit 1
            fi
            ok "Docker shared services match enabled BotSpecs."
        else
            if ! compose up -d "$@"; then
                err "Failed to start explicit service(s): $*"
                compose ps "$@" || true
                exit 1
            fi
            ok "Explicit service(s) started for diagnosis/login: $*"
        fi
        compose ps -a
        ;;
    stop)
        if [ "$#" -eq 0 ]; then
            compose down --remove-orphans
            ok "Docker shared-service project stopped."
        else
            compose stop "$@"
            ok "Explicit service(s) stopped: $*"
        fi
        ;;
    status)
        if [ "$#" -eq 0 ]; then
            print_desired_services || exit 1
            compose ps -a
        else
            compose ps -a "$@"
        fi
        ;;
    logs)
        compose logs -f "$@"
        ;;
    doctor)
        SERVICE="${1:-}"
        case "$SERVICE" in
            xhs|xiaohongshu) xhs_doctor ;;
            searxng|sx)
                doctor_rc=0
                transport_doctor "$SEARXNG_CONTAINER" "searxng" "$SEARXNG_URL/" '^(200)$' || doctor_rc=1
                searxng_probe || doctor_rc=1
                exit "$doctor_rc"
                ;;
            playwright|browser)
                doctor_rc=0
                transport_doctor "$PLAYWRIGHT_CONTAINER" "playwright-mcp" "$PLAYWRIGHT_URL" '^(200|202|400|405|406)$' || doctor_rc=1
                playwright_probe || doctor_rc=1
                exit "$doctor_rc"
                ;;
            all)
                doctor_rc=0
                load_desired_services || exit 1
                if [ "${#DESIRED_SERVICES[@]}" -eq 0 ]; then
                    ok "No desired Docker services to diagnose."
                    exit 0
                fi
                for svc in "${DESIRED_SERVICES[@]}"; do
                    info "=== $svc ==="
                    case "$svc" in
                        searxng) doctor_name="searxng" ;;
                        playwright-mcp) doctor_name="playwright" ;;
                        xiaohongshu-mcp) doctor_name="xhs" ;;
                        *) err "Unsupported desired service: $svc"; doctor_rc=1; continue ;;
                    esac
                    if ! bash "$0" doctor "$doctor_name"; then
                        doctor_rc=1
                    fi
                    echo
                done
                exit "$doctor_rc"
                ;;
            *) err "Unknown doctor target: $SERVICE (supported: searxng, xhs, playwright, all)"; exit 2 ;;
        esac
        ;;
    probe)
        SERVICE="${1:-}"
        [ $# -gt 0 ] && shift || true
        case "$SERVICE" in
            xhs|xiaohongshu) xhs_probe "$@" ;;
            searxng|sx) searxng_probe "$@" ;;
            playwright|browser) playwright_probe "$@" ;;
            *) err "Unknown probe target: $SERVICE (supported: xhs, searxng, playwright)"; exit 2 ;;
        esac
        ;;
    desired)
        print_desired_services
        ;;
    login)
        SERVICE="${1:-}"
        [ $# -gt 0 ] && shift || true
        case "$SERVICE" in
            xhs|xiaohongshu) xhs_login "$@" ;;
            *) err "Unknown login target: $SERVICE (only xhs requires login)"; exit 2 ;;
        esac
        ;;
    -h|--help)
        sed -n '2,12p' "$0"
        ;;
    *)
        err "Usage: services.sh {start|stop|status|logs|doctor|probe|login|desired} [args...]"
        exit 2
        ;;
esac
