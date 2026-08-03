#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly POLICY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT="$(cd -- "${AGENTSTRATA_REPO_ROOT:-$POLICY_ROOT}" && pwd)"
readonly GITLEAKS_VERSION="8.30.1"
readonly MODE="${1:-all}"

case "$(uname -m)" in
  x86_64|amd64)
    readonly ASSET_ARCH="x64"
    readonly ARCHIVE_SHA256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
    ;;
  aarch64|arm64)
    readonly ASSET_ARCH="arm64"
    readonly ARCHIVE_SHA256="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"
    ;;
  *)
    printf 'ERROR: unsupported architecture\n' >&2
    exit 2
    ;;
esac

case "$MODE" in
  all|history|changes) ;;
  *)
    printf 'usage: check_secrets.sh [all|history|changes]\n' >&2
    exit 2
    ;;
esac

readonly WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agentstrata-gitleaks.XXXXXX")"
cleanup() {
  rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT HUP INT TERM
chmod 0700 "$WORK_DIR"

readonly ARCHIVE="$WORK_DIR/gitleaks.tar.gz"
readonly DOWNLOAD_URL="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${ASSET_ARCH}.tar.gz"
readonly EMPTY_IGNORE="$WORK_DIR/controlled-empty.gitleaksignore"
: > "$EMPTY_IGNORE"
chmod 0600 "$EMPTY_IGNORE"

curl --fail --location --silent --show-error --retry 3 \
  --proto '=https' --tlsv1.2 \
  --output "$ARCHIVE" "$DOWNLOAD_URL"
printf '%s  %s\n' "$ARCHIVE_SHA256" "$ARCHIVE" | sha256sum --check --status
tar -xzf "$ARCHIVE" -C "$WORK_DIR" gitleaks
readonly GITLEAKS="$WORK_DIR/gitleaks"

run_gitleaks() {
  local scope="$1"
  shift
  local report="$WORK_DIR/${scope}.json"
  local status
  set +e
  "$GITLEAKS" "$@" \
    --config "$POLICY_ROOT/.gitleaks.toml" \
    --gitleaks-ignore-path "$EMPTY_IGNORE" \
    --ignore-gitleaks-allow \
    --max-archive-depth=2 \
    --max-decode-depth=2 \
    --no-banner \
    --redact=100 \
    --report-format=json \
    --report-path "$report" \
    > /dev/null 2>&1
  status=$?
  set -e
  case "$status" in
    0)
      return 0
      ;;
    1)
      printf 'FAIL: gitleaks %s scan detected findings; inspect a private local run\n' "$scope" >&2
      return 1
      ;;
    *)
      printf 'ERROR: gitleaks %s scan failed without publishing raw output\n' "$scope" >&2
      return 2
      ;;
  esac
}

scan_history() {
  run_gitleaks history git "$ROOT" --log-opts=--all
}

materialize_index() {
  local destination="$1"
  local before_tree after_tree symlink_list link_path target_file
  mkdir -p "$destination"
  before_tree="$(git -C "$ROOT" write-tree 2>/dev/null)" || return 2
  git -C "$ROOT" checkout-index --all --prefix="$destination/" \
    > /dev/null 2>&1 || return 2
  after_tree="$(git -C "$ROOT" write-tree 2>/dev/null)" || return 2
  [[ "$before_tree" == "$after_tree" ]] || return 2

  symlink_list="$WORK_DIR/index-symlinks.list"
  find "$destination" -type l -print0 > "$symlink_list" 2>/dev/null || return 2
  while IFS= read -r -d '' link_path; do
    target_file="$(mktemp "$WORK_DIR/index-link.XXXXXX")" || return 2
    readlink -- "$link_path" > "$target_file" 2>/dev/null || return 2
    chmod 0600 "$target_file" || return 2
    unlink -- "$link_path" 2>/dev/null || return 2
    mv -- "$target_file" "$link_path" 2>/dev/null || return 2
  done < "$symlink_list"
  find "$destination" -type d -exec chmod 0700 {} + 2>/dev/null || return 2
  find "$destination" -type f -exec chmod 0600 {} + 2>/dev/null || return 2
}

materialize_path_list() {
  local list_file="$1"
  local destination_root="$2"
  local deleted_list="${3:-}"
  local relative_path source_path destination deleted_path
  declare -A allowed_missing=()
  if [[ -n "$deleted_list" ]]; then
    while IFS= read -r -d '' deleted_path; do
      allowed_missing["$deleted_path"]=1
    done < "$deleted_list"
  fi
  while IFS= read -r -d '' relative_path; do
    source_path="$ROOT/$relative_path"
    destination="$destination_root/$relative_path"
    mkdir -p "$(dirname -- "$destination")" 2>/dev/null || return 2
    if [[ -L "$source_path" ]]; then
      readlink -- "$source_path" > "$destination" 2>/dev/null || return 2
    elif [[ -f "$source_path" ]]; then
      cp --no-dereference -- "$source_path" "$destination" 2>/dev/null || return 2
      [[ -f "$source_path" && ! -L "$source_path" ]] || return 2
      cmp --silent -- "$source_path" "$destination" 2>/dev/null || return 2
    elif [[ ! -e "$source_path" && -n "${allowed_missing[$relative_path]+present}" ]]; then
      continue
    elif [[ ! -e "$source_path" ]]; then
      return 2
    else
      return 2
    fi
    chmod 0600 "$destination" 2>/dev/null || return 2
  done < "$list_file"
}

materialize_worktree_scope() {
  local scope="$1"
  local destination="$2"
  shift 2
  local before_list="$WORK_DIR/${scope}-before.list"
  local after_list="$WORK_DIR/${scope}-after.list"
  local deleted_list=""
  if [[ "$scope" == "worktree" ]]; then
    deleted_list="$WORK_DIR/worktree-deleted.list"
    git -C "$ROOT" ls-files -z --deleted > "$deleted_list" 2>/dev/null || return 2
  fi
  git -C "$ROOT" ls-files -z "$@" > "$before_list" 2>/dev/null || return 2
  materialize_path_list "$before_list" "$destination" "$deleted_list" || return 2
  git -C "$ROOT" ls-files -z "$@" > "$after_list" 2>/dev/null || return 2
  cmp --silent -- "$before_list" "$after_list" 2>/dev/null || return 2
}

scan_changes() {
  local candidate_root="$WORK_DIR/candidates"
  mkdir -p "$candidate_root/index" "$candidate_root/worktree" "$candidate_root/untracked"
  if ! materialize_index "$candidate_root/index"; then
    printf 'ERROR: unable to build the private index snapshot\n' >&2
    return 2
  fi
  if ! materialize_worktree_scope \
    worktree "$candidate_root/worktree" --modified; then
    printf 'ERROR: unable to build the private worktree snapshot\n' >&2
    return 2
  fi
  if ! materialize_worktree_scope \
    untracked "$candidate_root/untracked" --others --exclude-standard; then
    printf 'ERROR: unable to build the private untracked snapshot\n' >&2
    return 2
  fi
  run_gitleaks changes dir "$candidate_root"
}

case "$MODE" in
  history)
    scan_history
    ;;
  changes)
    scan_changes
    ;;
  all)
    scan_history
    scan_changes
    ;;
esac