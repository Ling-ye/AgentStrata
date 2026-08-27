#!/usr/bin/env bash
# Verify the guided runtime in disposable supported-distribution containers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
NAPCAT_IMAGE="mlikiowa/napcat-docker@sha256:0b4b24114089bfbbefd4729ad08b50a6b9d67044aec674809ede3cf7521c4431"

ALL_IMAGES=(
    ubuntu:22.04
    ubuntu:24.04
    ubuntu:26.04
    debian:11
    debian:12
    debian:13
)
SELECTED_IMAGES=()
RUN_MATRIX=0
RUN_MANIFEST=1
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: bash scripts/verify_guided_runtime_matrix.sh [options]

Run the first-deployment runtime smoke in disposable Linux containers. The
script never starts AgentStrata, NapCat, systemd, or a QQ gateway. It only
pulls the requested base images and runtime artifacts into Docker's local
cache, then removes each test container.

Options:
  --all                 Run Ubuntu 22.04/24.04/26.04 and Debian 11/12/13.
  --image IMAGE         Run one supported image; may be repeated.
  --manifest-only       Check the pinned NapCat digest for amd64 and arm64 only.
  --skip-manifest       Do not perform the read-only NapCat manifest check.
  --dry-run             Print selected work without invoking Docker.
  -h, --help            Show this help.

Examples:
  bash scripts/verify_guided_runtime_matrix.sh --image ubuntu:24.04
  bash scripts/verify_guided_runtime_matrix.sh --all
  bash scripts/verify_guided_runtime_matrix.sh --manifest-only
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --all) RUN_MATRIX=1; SELECTED_IMAGES=("${ALL_IMAGES[@]}") ;;
        --image)
            [ "$#" -ge 2 ] || { echo "[ERR] --image needs a value" >&2; exit 2; }
            SELECTED_IMAGES+=("$2")
            RUN_MATRIX=1
            shift
            ;;
        --manifest-only) RUN_MATRIX=0; RUN_MANIFEST=1 ;;
        --skip-manifest) RUN_MANIFEST=0 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[ERR] unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ "$RUN_MATRIX" -eq 0 ] && [ "$RUN_MANIFEST" -eq 0 ]; then
    echo "[ERR] select --all, --image, or omit --skip-manifest" >&2
    exit 2
fi

is_supported_image() {
    local candidate="$1" image
    for image in "${ALL_IMAGES[@]}"; do
        [ "$candidate" = "$image" ] && return 0
    done
    return 1
}

for image in "${SELECTED_IMAGES[@]}"; do
    is_supported_image "$image" || {
        echo "[ERR] unsupported matrix image: $image" >&2
        exit 2
    }
done

require_local_docker() {
    command -v docker >/dev/null 2>&1 || {
        echo "[ERR] Docker is required for the disposable matrix" >&2
        exit 1
    }
    docker info >/dev/null 2>&1 || {
        echo "[ERR] docker info is unavailable; do not use a remote daemon for this matrix" >&2
        exit 1
    }
    local endpoint context
    if [ -n "${DOCKER_HOST:-}" ]; then
        endpoint="$DOCKER_HOST"
    else
        context="$(docker context show 2>/dev/null)" || {
            echo "[ERR] cannot determine Docker context" >&2
            exit 1
        }
        endpoint="$(docker context inspect "$context" --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" || {
            echo "[ERR] cannot determine Docker endpoint" >&2
            exit 1
        }
    fi
    case "$endpoint" in
        unix://*) ;;
        *)
            echo "[ERR] refusing non-local Docker endpoint: $endpoint" >&2
            exit 1
            ;;
    esac
}

check_napcat_manifest() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[DRY-RUN] docker buildx imagetools inspect --raw $NAPCAT_IMAGE"
        return 0
    fi
    command -v python3 >/dev/null 2>&1 || {
        echo "[ERR] python3 is required to inspect the multi-arch manifest" >&2
        return 1
    }
    local manifest
    manifest="$(docker buildx imagetools inspect --raw "$NAPCAT_IMAGE")" || {
        echo "[ERR] unable to read pinned NapCat manifest" >&2
        return 1
    }
    NAPCAT_MANIFEST="$manifest" python3 - <<'PY'
import json
import os
import sys

try:
    document = json.loads(os.environ["NAPCAT_MANIFEST"])
except (KeyError, json.JSONDecodeError):
    raise SystemExit("[ERR] pinned NapCat reference did not return JSON")

manifests = document.get("manifests")
if not isinstance(manifests, list):
    raise SystemExit("[ERR] pinned NapCat reference is not a multi-architecture index")
platforms = {
    (item.get("platform") or {}).get("architecture")
    for item in manifests
    if isinstance(item, dict) and (item.get("platform") or {}).get("os") == "linux"
}
missing = {"amd64", "arm64"} - platforms
if missing:
    raise SystemExit("[ERR] pinned NapCat manifest lacks linux/" + ", linux/".join(sorted(missing)))
print("[OK] pinned NapCat manifest includes linux/amd64 and linux/arm64")
PY
}

run_one_image() {
    local image="$1"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[DRY-RUN] docker run --rm --pull=always --network=bridge -v %q:/source:ro %q ...\n' \
            "$REPO_ROOT" "$image"
        return 0
    fi

    echo "[matrix] $image"
    docker run --rm --pull=always --network=bridge \
        -v "$REPO_ROOT:/source:ro" \
        "$image" bash -lc '
            set -euo pipefail
            export DEBIAN_FRONTEND=noninteractive
            apt-get update
            apt-get install -y --no-install-recommends ca-certificates curl xz-utils
            rm -rf /var/lib/apt/lists/*
            useradd --home-dir /tmp/agentstrata-home --create-home --shell /bin/bash agentstrata
            mkdir -p /work
            tar \
              --exclude=.git \
              --exclude=.venv \
              --exclude=.worktrees \
              --exclude=console/web/node_modules \
              --exclude=__pycache__ \
              -C /source -cf - . | tar -C /work -xf -
            chown -R agentstrata:agentstrata /work
            su -s /bin/bash agentstrata -c "
              set -euo pipefail
              cd /work
              export HOME=/tmp/agentstrata-home
              export AGENTSTRATA_RUNTIME_ROOT=\$HOME/.local/share/agentstrata
              bash deploy/wsl/install_wsl_env.sh --no-system-packages
              PYTHONPATH=src .venv/bin/python -c '\''from chatcopilot.run import main; from chatcopilot.middleware.acp.server import main as acp_main; print(\"runtime imports ok\")'\''
              .venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
            "
        '
    echo "[OK] $image isolated runtime/import/BotSpec smoke passed"
}

if [ "$DRY_RUN" -eq 0 ]; then
    require_local_docker
fi

if [ "$RUN_MANIFEST" -eq 1 ]; then
    check_napcat_manifest
fi

for image in "${SELECTED_IMAGES[@]}"; do
    run_one_image "$image"
done

if [ "$RUN_MATRIX" -eq 1 ]; then
    echo "[OK] requested guided-runtime matrix passed"
fi
