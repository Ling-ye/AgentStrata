#!/usr/bin/env bash
# Manage shared Docker infrastructure services.
#
# Usage:
#   bash deploy/docker/services.sh start
#   bash deploy/docker/services.sh stop
#   bash deploy/docker/services.sh status
#   bash deploy/docker/services.sh logs [service]
#   bash deploy/docker/services.sh doctor all
#   bash deploy/docker/services.sh doctor tavily
#   bash deploy/docker/services.sh doctor xhs
#   bash deploy/docker/services.sh probe xhs --keyword "青山制面 上海"
#   bash deploy/docker/services.sh login xhs --qrcode
#   bash deploy/docker/services.sh login xhs --qrcode --output /tmp/xhs.png
#   bash deploy/docker/services.sh login xhs --qrcode --print-data-url
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
ACTION="${1:-}"
[ $# -gt 0 ] && shift || true

XHS_CONTAINER="chatcopilot-xiaohongshu-mcp"
XHS_PORT="${XHS_MCP_PORT:-18060}"
XHS_URL="http://localhost:${XHS_PORT}/mcp"

TAVILY_CONTAINER="chatcopilot-tavily-mcp"
TAVILY_PORT="${TAVILY_MCP_PORT:-18061}"
TAVILY_URL="http://localhost:${TAVILY_PORT}/mcp"

SEQ_THINKING_CONTAINER="chatcopilot-sequential-thinking-mcp"
SEQ_THINKING_PORT="${SEQ_THINKING_MCP_PORT:-18062}"
SEQ_THINKING_URL="http://localhost:${SEQ_THINKING_PORT}/mcp"

TAOKE_CONTAINER="chatcopilot-taoke-mcp"
TAOKE_PORT="${TAOKE_MCP_PORT:-18063}"
TAOKE_URL="http://localhost:${TAOKE_PORT}/mcp"

SEARXNG_CONTAINER="chatcopilot-searxng-mcp"
SEARXNG_PORT="${SEARXNG_MCP_PORT:-18065}"
SEARXNG_URL="http://localhost:${SEARXNG_PORT}/mcp"

PLAYWRIGHT_CONTAINER="chatcopilot-playwright-mcp"
PLAYWRIGHT_PORT="${PLAYWRIGHT_MCP_PORT:-18066}"
PLAYWRIGHT_URL="http://localhost:${PLAYWRIGHT_PORT}/mcp"

ALL_SERVICES="tavily sequential-thinking searxng xiaohongshu playwright taoke"

ok()   { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
info() { printf "\033[1;36m[*]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ERR]\033[0m %s\n" "$*" >&2; }

compose() {
    docker compose -f "$SCRIPT_DIR/docker-compose.yaml" "$@"
}

require_xhs_running() {
    if ! docker ps --format '{{.Names}}' | grep -Fxq "$XHS_CONTAINER"; then
        err "Container $XHS_CONTAINER is not running. Run 'services.sh start' first."
        return 1
    fi
}

json_string() {
    python3 -c 'import json,sys; print(json.dumps(sys.argv[1], ensure_ascii=False))' "$1"
}

xhs_mcp_call() {
    local tool_name="$1"
    local args_json="${2:-}"
    local timeout_s="${3:-120}"
    local hdr_file payload_json
    if [ -z "$args_json" ]; then
        args_json="{}"
    fi
    hdr_file="$(mktemp)"

    curl -sfS --max-time "$timeout_s" -D "$hdr_file" -o /dev/null -X POST "$XHS_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"chatcopilot-services","version":"1.0"}}}'

    local sid
    sid="$(grep -i '^mcp-session-id:' "$hdr_file" | tr -d '\r' | awk '{print $2}')"
    rm -f "$hdr_file"

    local sid_args=()
    if [ -n "$sid" ]; then
        sid_args=(-H "Mcp-Session-Id: $sid")
    fi

    curl -sfS --max-time "$timeout_s" -o /dev/null -X POST "$XHS_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        "${sid_args[@]}" \
        -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

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
    curl -sfS --max-time "$timeout_s" -X POST "$XHS_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        "${sid_args[@]}" \
        --data-binary @-
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

generic_doctor() {
    local container="$1"
    local compose_svc="$2"
    local url="$3"
    local rc=0

    info "Checking Docker container $container..."
    if docker ps --format '{{.Names}}' | grep -Fxq "$container"; then
        ok "$container is running."
    else
        err "$container is not running."
        rc=1
    fi

    info "Checking compose status..."
    compose ps "$compose_svc" || rc=1

    info "Checking HTTP endpoint $url..."
    if curl -sS --max-time 5 -o /dev/null -w "%{http_code}\n" "$url" | grep -Eq '^(200|202|400|405|406)$'; then
        ok "MCP endpoint responds."
    else
        warn "MCP endpoint did not respond with an expected status."
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
    info "Checking Docker container..."
    if require_xhs_running; then
        ok "$XHS_CONTAINER is running."
    else
        rc=1
    fi

    info "Checking compose status..."
    compose ps xiaohongshu-mcp || rc=1

    info "Checking HTTP endpoint $XHS_URL..."
    if curl -sS --max-time 5 -o /dev/null -w "%{http_code}\n" "$XHS_URL" | grep -Eq '^(200|202|400|405)$'; then
        ok "MCP endpoint responds."
    else
        warn "MCP endpoint did not respond with an expected status."
        rc=1
    fi

    if docker ps --format '{{.Names}}' | grep -Fxq "$XHS_CONTAINER"; then
        info "Checking container DNS..."
        docker exec "$XHS_CONTAINER" sh -lc 'getent hosts www.xiaohongshu.com xiaohongshu.com >/tmp/xhs_dns.out 2>/tmp/xhs_dns.err; cat /tmp/xhs_dns.out; cat /tmp/xhs_dns.err >&2'

        info "Checking key environment variables..."
        docker exec "$XHS_CONTAINER" sh -lc 'printenv | sort | grep -E "^(COOKIES_PATH|HOME|XDG_CACHE_HOME|XDG_CONFIG_HOME|XHS_PROXY)=" || true'

        info "Checking cookie and browser profile files..."
        docker exec "$XHS_CONTAINER" sh -lc '
            for path in "${COOKIES_PATH:-/app/data/cookies.json}" /app/data/home /app/data/cache /app/data/config; do
                if [ -e "$path" ]; then
                    ls -ld "$path"
                else
                    echo "missing: $path"
                fi
            done
        '

        info "Recent errors from container logs..."
        docker logs --tail 200 "$XHS_CONTAINER" 2>&1 |
            grep -Ei "error|panic|timeout|ERR_|not resolved|登录|cookie|search|搜索" |
            tail -60 || true
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

stateless_mcp_call() {
    local url="$1"
    local tool_name="$2"
    local args_json="${3:-{}}"
    python3 - "$url" "$tool_name" "$args_json" <<'PY'
import json
import sys
import urllib.request

url, tool, raw_args = sys.argv[1], sys.argv[2], sys.argv[3]
args = json.loads(raw_args or "{}")
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": tool, "arguments": args},
}
request = urllib.request.Request(
    url,
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json", "Accept": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(response.read().decode("utf-8", errors="replace"))
PY
}

searxng_probe() {
    local keyword="Tobiichi Origami wallpaper"
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

    info "Searching SearXNG images with keyword: $keyword"
    python3 - "$SEARXNG_URL" "$keyword" <<'PY'
import json
import sys
import urllib.request

url, keyword = sys.argv[1], sys.argv[2]
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "image_search", "arguments": {"query": keyword, "limit": 5}},
}
request = urllib.request.Request(
    url,
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json", "Accept": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        outer = json.loads(response.read().decode("utf-8", errors="replace"))
except Exception as exc:
    print(f"SearXNG image_search failed: {exc}", file=sys.stderr)
    raise SystemExit(1)
content = outer.get("result", {}).get("content") or []
text = content[0].get("text", "{}") if content else "{}"
payload = json.loads(text)
if not payload.get("ok"):
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(1)
candidates = payload.get("image_candidates") or []
print(json.dumps(
    {
        "ok": True,
        "query": payload.get("query"),
        "candidate_count": len(candidates),
        "image_candidates": candidates[:5],
    },
    ensure_ascii=False,
    indent=2,
))
PY
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
    start)
        if ! compose up -d "$@"; then
            err "Failed to start services."
            compose ps "$@" || true
            exit 1
        fi
        ok "Services started."
        compose ps
        ;;
    stop)
        compose down "$@"
        ok "Services stopped."
        ;;
    status)
        compose ps "$@"
        ;;
    logs)
        compose logs -f "$@"
        ;;
    doctor)
        SERVICE="${1:-}"
        case "$SERVICE" in
            xhs|xiaohongshu) xhs_doctor ;;
            tavily) generic_doctor "$TAVILY_CONTAINER" "tavily-mcp" "$TAVILY_URL" ;;
            seq|sequential-thinking) generic_doctor "$SEQ_THINKING_CONTAINER" "sequential-thinking-mcp" "$SEQ_THINKING_URL" ;;
            searxng|sx) generic_doctor "$SEARXNG_CONTAINER" "searxng-mcp" "$SEARXNG_URL" ;;
            playwright|browser) generic_doctor "$PLAYWRIGHT_CONTAINER" "playwright-mcp" "$PLAYWRIGHT_URL" ;;
            taoke) generic_doctor "$TAOKE_CONTAINER" "taoke-mcp" "$TAOKE_URL" ;;
            all)
                doctor_rc=0
                for svc in $ALL_SERVICES; do
                    info "=== $svc ==="
                    if ! bash "$0" doctor "$svc"; then
                        doctor_rc=1
                    fi
                    echo
                done
                exit "$doctor_rc"
                ;;
            *) err "Unknown doctor target: $SERVICE (supported: tavily, sequential-thinking, searxng, xhs, playwright, taoke, all)"; exit 2 ;;
        esac
        ;;
    probe)
        SERVICE="${1:-}"
        [ $# -gt 0 ] && shift || true
        case "$SERVICE" in
            xhs|xiaohongshu) xhs_probe "$@" ;;
            searxng|sx) searxng_probe "$@" ;;
            *) err "Unknown probe target: $SERVICE (supported: xhs, searxng; tavily/sequential-thinking/taoke are stateless)"; exit 2 ;;
        esac
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
        err "Usage: services.sh {start|stop|status|logs|doctor|probe|login} [args...]"
        exit 2
        ;;
esac
