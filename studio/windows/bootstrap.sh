#!/usr/bin/env bash
set -euo pipefail
umask 077
if [[ $1 == system ]]; then
  [[ $(ps -o comm= -p 1) == systemd ]] || { echo 'WSL2 requires systemd' >&2; exit 1; }
  if ! id -u agentstrata-studio >/dev/null 2>&1; then useradd -m -s /bin/bash agentstrata-studio; fi
  if ! command -v bwrap >/dev/null || ! command -v rsync >/dev/null || ! command -v git >/dev/null || ! command -v rg >/dev/null; then
    apt-get update >&2
    DEBIAN_FRONTEND=noninteractive apt-get install -y git curl bubblewrap rsync ripgrep ca-certificates build-essential xz-utils unzip >&2
  fi
  loginctl enable-linger agentstrata-studio
  systemctl start "user@$(id -u agentstrata-studio).service"
  exit
fi
resources=$2
state="$HOME/.local/share/agentstrata/studio"
mkdir -p "$state/platform"; chmod 700 "$state"
read -r digest < "$resources/framework.sha256"
[[ $digest =~ ^[0-9a-f]{64}$ ]]
source_dir="$state/platform/$digest"
if [[ ! -e "$source_dir" ]]; then
  [[ $(sha256sum "$resources/framework.tar.gz" | cut -d' ' -f1) == "$digest" ]]
  pending=$(mktemp -d "$state/platform/.extract-XXXXXXXX")
  tar -xf "$resources/framework.tar.gz" -C "$pending" --no-same-owner
  mv -T "$pending" "$source_dir"
fi
cd "$source_dir"
if [[ ! -e "$source_dir/.studio-ready" ]]; then
  bash deploy/wsl/install_wsl_env.sh --no-system-packages --with-console-deps --with-node --no-verify >&2
  uv_bin="$HOME/.local/share/agentstrata/uv/0.12.5/bin/uv"
  "$uv_bin" sync --locked --active --extra agent --extra acp --extra dev --extra evaluation --extra weixin >&2
  node_dir=$(find "$HOME/.local/share/agentstrata/node" -type f -name node -executable -print -quit)
  export PATH="$(dirname "$node_dir"):$PATH"
  npm ci --prefix console/web --no-audit --no-fund >&2
  .venv/bin/python -m studio.install "$state"
  touch "$source_dir/.studio-ready"
fi
node_dir=$(find "$HOME/.local/share/agentstrata/node" -type f -name node -executable -print -quit)
export PATH="$(dirname "$node_dir"):$HOME/.local/share/agentstrata/uv/0.12.5/bin:$PATH"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
unit="agentstrata-studio-web"
if ! systemctl --user -q is-active "$unit"; then
  systemd-run --user --quiet --collect --unit="$unit" --property=UMask=0077 --working-directory="$source_dir" \
    --setenv="PYTHONPATH=$source_dir/src:$source_dir" --setenv="WSL_DISTRO_NAME=${WSL_DISTRO_NAME:-Ubuntu-24.04}" \
    --setenv="WSL_INTEROP=${WSL_INTEROP:-}" --setenv="PATH=$PATH" "$source_dir/.venv/bin/python" -m studio serve --home "$state"
fi
for i in {1..90}; do
  if curl --silent --fail -o /dev/null http://127.0.0.1:18636/; then
    "$source_dir/.venv/bin/python" -m studio url --home "$state"
    # A live Windows-owned WSL client retains interop even after the browser closes.
    while systemctl --user -q is-active "$unit"; do sleep 12; done
    exit
  fi
  sleep 1
done
echo 'Local Studio did not become ready' >&2; exit 1
