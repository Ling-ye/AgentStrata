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
state="${AGENTSTRATA_STUDIO_HOME:-$HOME/.local/share/agentstrata/studio}"
export AGENTSTRATA_STUDIO_HOME="$state"
mkdir -p "$state/platform"; chmod 700 "$state"
read -r digest < "$resources/framework.sha256"
[[ $digest =~ ^[0-9a-f]{64}$ ]]
source_dir="$state/platform/$digest"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
unit="agentstrata-studio-web"
# Serialize launcher preparation, not the lifetime of the web service.
exec 9>"$state/launcher.lock"
flock -x 9
if systemctl --user -q is-active "$unit"; then
  running_source=$(systemctl --user show "$unit" --property=WorkingDirectory --value)
  if [[ "$running_source" != "$source_dir" ]]; then
    echo 'Another Studio version is still running. Finish or cancel its development/build tasks, then explicitly stop the old workbench before opening this update. No running service or project was changed.' >&2
    exit 1
  fi
  # An old ready marker does not prove this version contains the unified UI.
  if [[ ! -f "$source_dir/.studio-ready" || ! -f "$source_dir/console/web/dist/index.html" ]]; then
    echo 'The running Studio needs its frontend prepared. Finish its tasks and explicitly stop the workbench before retrying.' >&2
    exit 1
  fi
fi
# A stopped web process can still own recoverable development/build/import work.
# Resume it with its previously prepared version instead of changing its code.
previous_digest=""
if [[ -f "$state/framework-version" ]]; then read -r previous_digest < "$state/framework-version"; fi
consumer_guard=no
if [[ "$previous_digest" != "$digest" || ! -f "$source_dir/.studio-ready" || ! -f "$source_dir/console/web/dist/index.html" ]]; then
  # The web unit may have stopped while an in-flight development step is still
  # finishing. Share the consumer's real lock through preparation and readiness.
  # Append-open preserves existing bytes; validate the inherited fd before use.
  [[ ! -L "$state/development-worker.lock" ]] || { echo 'Cannot verify Studio development consumer ownership; no preparation was started.' >&2; exit 1; }
  exec 8>>"$state/development-worker.lock"
  python3 - "$state/development-worker.lock" <<'PY'
import os
import stat
import sys
try:
    opened = os.fstat(8)
    named = os.stat(sys.argv[1], follow_symlinks=False)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid() or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600 or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
        raise ValueError("unsafe ownership")
except (OSError, ValueError):
    sys.exit("Cannot verify Studio development consumer ownership; no preparation was started.")
PY
  if ! flock -n -x 8; then
    echo 'A Studio development consumer still owns this project library. Let the previous workbench finish shutting down, then retry. No task, frontend or version record was changed.' >&2
    exit 1
  fi
  consumer_guard=yes
fi
if [[ -f "$state/studio.sqlite" && "$consumer_guard" == yes ]]; then
  python3 - "$state/studio.sqlite" "$previous_digest" "$digest" <<'PY'
import sqlite3
import sys
from pathlib import Path
try:
    database = Path(sys.argv[1])
    # SQLite mode=ro can still create or update WAL/SHM sidecars. Do not open
    # WAL storage or ignore its uncheckpointed work via an immutable fallback.
    with database.open("rb") as source:
        header = source.read(20)
    if len(header) != 20 or header[:16] != b"SQLite format 3\0":
        raise ValueError("Unrecognized Studio database header")
    if 2 in header[18:20]:
        sys.exit("Cannot verify unfinished Studio tasks/imports in WAL-mode storage without changing SQLite sidecars. Use the previous version to inspect and obtain a consistent project library before upgrading. No project or version record was changed.")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        pending = db.execute("SELECT COUNT(*) FROM messages WHERE state IN "
                             "('thinking','console-thinking','starting','developing','verifying','packaging')").fetchone()[0]
        unknown = db.execute("SELECT COUNT(*) FROM messages WHERE state IS NULL OR state NOT IN "
                             "('thinking','console-thinking','starting','developing','verifying','packaging',"
                             "'ready','failed','discuss','configure','applied','new_project')").fetchone()[0]
        imports_pending = 0
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_imports'").fetchone():
            settled = ("imported", "cancelled", "failed")
            if sys.argv[2] == sys.argv[3]:
                # A paused import can be explicitly recovered by the same code;
                # active selection/copy and unknown states still block rebuilding.
                settled += ("recovery_required",)
            imports_pending = db.execute("SELECT COUNT(*) FROM studio_imports WHERE state NOT IN (" +
                                         ",".join("?" for _ in settled) + ") OR state IS NULL", settled).fetchone()[0]
except (sqlite3.Error, OSError, ValueError):
    sys.exit("Cannot verify unfinished Studio tasks/imports. Upgrade was stopped without changing projects; use the previous version to inspect and resolve its state.")
if unknown:
    sys.exit("Studio has an unrecognized development state. Reopen the previous version to inspect or resolve its tasks before upgrading. No project or version record was changed.")
if imports_pending:
    sys.exit("Studio has unfinished or interrupted Live2D imports. This version cannot safely resume their source selection or copy. Reopen the previous version and explicitly finish or resolve those imports before upgrading. No import was retried and projects were not changed.")
if pending:
    sys.exit("Studio has unfinished development/build tasks. Reopen the previous version to finish or recover them before upgrading. Projects were not changed.")
PY
fi
if [[ ! -e "$source_dir" ]]; then
  [[ $(sha256sum "$resources/framework.tar.gz" | cut -d' ' -f1) == "$digest" ]]
  pending=$(mktemp -d "$state/platform/.extract-XXXXXXXX")
  tar -xf "$resources/framework.tar.gz" -C "$pending" --no-same-owner
  mv -T "$pending" "$source_dir"
fi
cd "$source_dir"
if [[ ! -e "$source_dir/.studio-ready" || ! -f "$source_dir/console/web/dist/index.html" ]]; then
  bash deploy/wsl/install_wsl_env.sh --no-system-packages --with-console-deps --with-node --no-verify >&2
  uv_bin="$HOME/.local/share/agentstrata/uv/0.12.5/bin/uv"
  "$uv_bin" sync --locked --active --extra agent --extra acp --extra dev --extra evaluation --extra weixin >&2
  node_dir=$(find "$HOME/.local/share/agentstrata/node" -type f -name node -executable -print -quit)
  export PATH="$(dirname "$node_dir"):$PATH"
  # New installers carry the production frontend built with their source.
  # Older source-only archives retain the original local build fallback.
  if [[ ! -f console/web/dist/index.html ]]; then
    npm ci --prefix console/web --no-audit --no-fund >&2
    npm run build --prefix console/web >&2
  fi
  [[ -f console/web/dist/index.html ]] || { echo 'Studio frontend build is missing' >&2; exit 1; }
  .venv/bin/python -m studio.install "$state"
  touch "$source_dir/.studio-ready"
fi
node_dir=$(find "$HOME/.local/share/agentstrata/node" -type f -name node -executable -print -quit)
export PATH="$(dirname "$node_dir"):$HOME/.local/share/agentstrata/uv/0.12.5/bin:$PATH"
if ! systemctl --user -q is-active "$unit"; then
  systemd-run --user --quiet --collect --unit="$unit" --property=UMask=0077 --working-directory="$source_dir" \
    --setenv="AGENTSTRATA_STUDIO_HOME=$state" --setenv="PYTHONPATH=$source_dir/src:$source_dir" --setenv="WSL_DISTRO_NAME=${WSL_DISTRO_NAME:-Ubuntu-24.04}" \
    --setenv="WSL_INTEROP=${WSL_INTEROP:-}" --setenv="PATH=$PATH" "$source_dir/.venv/bin/python" -m studio serve --home "$state"
fi
for i in {1..90}; do
  # A response on the fixed port can belong to another process. Verify the
  # service PID and a fresh token-bound proof before recording this version.
  if server_pid=$(systemctl --user show "$unit" --property=MainPID --value 2>/dev/null) &&
     [[ $server_pid =~ ^[1-9][0-9]*$ ]] &&
     url=$("$source_dir/.venv/bin/python" -m studio url --home "$state" --expected-pid "$server_pid" 2>/dev/null); then
    printf '%s\n' "$digest" > "$state/framework-version.next"
    mv -T "$state/framework-version.next" "$state/framework-version"
    printf '%s\n' "$url"
    if [[ "$consumer_guard" == yes ]]; then flock -u 8; fi
    flock -u 9
    # A live Windows-owned WSL client retains interop even after the browser closes.
    while systemctl --user -q is-active "$unit"; do sleep 12; done
    exit
  fi
  sleep 1
done
echo 'Local Studio did not become ready' >&2; exit 1
