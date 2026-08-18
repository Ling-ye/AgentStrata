#!/usr/bin/env bash
# dump.sh — 一次性把 WSL 端"调试物料"（配置 / 记忆 / 提示词 / 对话历史 / 日志 /
#           运行时状态 / 事故现场元信息）快照到 WSL 源仓的 _wsl_debug/<时间戳>/。
#
# 与其它脚本的边界：
#   - 控制台「日志」按钮         实时跟流（SSE），机器人正在跑想看新日志
#   - update_instance.sh       WSL 源仓 → 实例副本，同步代码并重启实例
#   - start.sh --apply-config  渲染 cc-connect 配置并启动
#   - dump.sh                  唯一权威"出 bug 一键全拍"入口（控制台「诊断」按钮调它）
#
# 输出布局（多实例时按 instance_id 分子目录，单实例同样保持，路径一致）：
#   _wsl_debug/<YYYY-MM-DD_HHMMSS>/
#   ├── _meta.json                    本次 dump 全局元信息（拍了哪些实例 / 选项 / 主机）
#   └── <instance_id>/
#       ├── configs/                  cc-connect.config.toml + .chatcopilot.env (+ .sanitized)
#       │                             （不复制实例私有 session attestation state/lock）
#       ├── memories/<user_dir>/      MEMORY.md（per-user）
#       ├── transcripts/<user_dir>/   *.jsonl 对话历史（per-session，full 模式才拍）
#       ├── prompts/                  persona.py 副本 + system prompt（full 模式才拍）
#       ├── logs/
#       │   ├── cc-connect/<date>.log     新位置（start.sh 改造后）；老 /tmp/cc-connect.log 也兜底拍
#       │   ├── runtime/<date>.log        ACP runtime 的 chatcopilot.* logger 独立文件
#       │   ├── questions/<date>.log      用户提问精简日志
#       │   ├── raw/<date>.log            用户提问 raw（CC_HOOK_* 全量）
#       │   └── _hook_errors.log
#       ├── runtime/
#       │   ├── status.txt                bash status.sh 输出
#       │   ├── processes.txt             pgrep -af cc-connect + ps
#       │   ├── tools_schema.json         build_tools_schema() 序列化（full 模式才生成）
#       │   ├── attachments_manifest.json 用户 attachments/downloads/results/uploads 文件级清单
#       │   │                             （只含 path/size/mtime/sha256，不拷文件本身）
#       │   ├── versions.txt              git rev / branch / dirty / pip freeze / cc-connect / node
#       │   ├── system.txt                uname / free / df / wsl.conf / PATH / timezone
#       │   ├── network.txt               ss -tulpn / LiteLLM 探活 / DNS 解析
#       │   ├── process_detail.txt        /proc/<pid>/status + lsof + cmdline（per pid）
#       │   └── timeline.tsv              cc-connect / runtime / questions 三流合并按 ts 排序
#       ├── manifest.json                 每个文件的 path / size / sha256 / mtime
#       └── README.md                     导航说明
#
# 用法（WSL 终端）：
#   bash dump.sh                              # full 模式，当前实例
#   bash dump.sh --mode quick                 # 仅 logs+configs+versions（30s 内）
#   bash dump.sh --instance lingye-copilot-qq,sample-bot
#   bash dump.sh --all-running                # 自动发现本机所有跑着的 cc-connect 实例
#   bash dump.sh --tail-lines 5000            # 日志只拷最后 N 行（quick 模式默认 5000）
#   bash dump.sh --dry-run                    # 只列将要拷的清单
#   bash dump.sh --out /tmp/foo               # 自定义输出根
#   bash dump.sh --include-env                # 显式包含 .chatcopilot.env 原文（默认不包含）
#   bash dump.sh --users ou_a,ou_b            # 只 dump 指定 user_id 的 memories/transcripts
#   bash dump.sh --archive                    # 完成后 tar.gz 打包

set -uo pipefail

# shellcheck source=./_load_env.sh
source "$(dirname "$0")/_load_env.sh"

# ----------------------------------------------------------------------------
# 1. 命令行参数
# ----------------------------------------------------------------------------
MODE="full"                    # full | quick
TAIL_LINES=0                   # 0 = 不裁剪
INSTANCES_OPT=""
ALL_RUNNING=0
OUT_ROOT_OVERRIDE=""
DRY_RUN=0
INCLUDE_ENV=0                  # 安全默认：只生成脱敏配置，不复制原始 env
USERS_FILTER=""
ARCHIVE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --mode) MODE="${2:-full}"; shift 2 ;;
        --quick) MODE="quick"; shift ;;
        --full) MODE="full"; shift ;;
        --tail-lines) TAIL_LINES="${2:-0}"; shift 2 ;;
        --instance|--instances) INSTANCES_OPT="${2:-}"; shift 2 ;;
        --all-running) ALL_RUNNING=1; shift ;;
        --out) OUT_ROOT_OVERRIDE="${2:-}"; shift 2 ;;
        --dry-run|-n) DRY_RUN=1; shift ;;
        --no-include-env) INCLUDE_ENV=0; shift ;;
        --include-env) INCLUDE_ENV=1; shift ;;
        --users) USERS_FILTER="${2:-}"; shift 2 ;;
        --archive) ARCHIVE=1; shift ;;
        -h|--help) sed -n '2,55p' "$0"; exit 0 ;;
        *) echo "[ERR] 未知参数：$1（--help 看用法）" >&2; exit 2 ;;
    esac
done

# quick 模式默认裁剪日志，避免拍历史几百 MB 的 cc-connect.log
if [ "$MODE" = "quick" ] && [ "$TAIL_LINES" = 0 ]; then
    TAIL_LINES=5000
fi

# ----------------------------------------------------------------------------
# 2. 样式 / 工具函数
# ----------------------------------------------------------------------------
step() { printf "\033[1;36m[%s]\033[0m %s\n" "$(date +%H:%M:%S)" "$*"; }
ok()   { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ERR]\033[0m %s\n" "$*" >&2; }
dim()  { printf "      \033[2m%s\033[0m\n" "$*"; }

if [ "$INCLUDE_ENV" = 1 ]; then
    warn "--include-env 会把原始运行时 env 写入快照；仅在明确需要且妥善保管输出时使用"
fi

# 把 USERS_FILTER 解析成精确匹配的 user_id 列表
USERS_PIPE=""
if [ -n "$USERS_FILTER" ]; then
    USERS_PIPE=":$(echo "$USERS_FILTER" | tr ',' ':' | tr -d ' '):"
fi

# ----------------------------------------------------------------------------
# 3. 实例发现
# ----------------------------------------------------------------------------

# 扫所有 cc-connect 进程的 /proc/<pid>/environ，抽出 CHATCOPILOT_INSTANCE_ID 去重。
# start.sh 通过 `exec env HOME=$CC_HOME cc-connect` 拉起，未传 -i，所以当前 shell 的
# CHATCOPILOT_INSTANCE_ID 会被继承到 cc-connect 的 environ 里，可靠回查。
discover_running_instances() {
    local pid environ instance
    {
        while IFS= read -r pid; do
            [ -z "$pid" ] && continue
            [ -r "/proc/$pid/environ" ] || continue
            environ="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null || true)"
            instance="$(echo "$environ" | awk -F= '/^CHATCOPILOT_INSTANCE_ID=/{print $2; exit}')"
            [ -n "$instance" ] && echo "$instance"
        done < <(pgrep -x cc-connect 2>/dev/null || pgrep -f cc-connect 2>/dev/null || true)
    } | sort -u
}

if [ "$ALL_RUNNING" = 1 ]; then
    INSTANCES_LIST="$(discover_running_instances | tr '\n' ' ')"
    if [ -z "${INSTANCES_LIST// }" ]; then
        warn "--all-running 未发现任何运行中的 cc-connect 实例；回退到当前 shell 实例"
        INSTANCES_LIST="${CHATCOPILOT_INSTANCE_ID:-default}"
    fi
elif [ -n "$INSTANCES_OPT" ]; then
    INSTANCES_LIST="$(echo "$INSTANCES_OPT" | tr ',' ' ')"
else
    INSTANCES_LIST="${CHATCOPILOT_INSTANCE_ID:-default}"
fi

# ----------------------------------------------------------------------------
# 4. 决定输出根
# ----------------------------------------------------------------------------
TS="$(date +%Y-%m-%d_%H%M%S)"
SOURCE_REPO_DEFAULT="${CHATCOPILOT_DUMP_ROOT:-$HOME/ChatCopilot}"
if [ -n "$OUT_ROOT_OVERRIDE" ]; then
    OUT_BASE="$OUT_ROOT_OVERRIDE"
elif [ -d "$SOURCE_REPO_DEFAULT" ]; then
    OUT_BASE="$SOURCE_REPO_DEFAULT/_wsl_debug/$TS"
else
    OUT_BASE="/tmp/chatcopilot-dump-$TS"
    warn "WSL 源仓路径不可见（CHATCOPILOT_DUMP_ROOT=$SOURCE_REPO_DEFAULT），落到 $OUT_BASE"
fi

step "Mode=$MODE  TailLines=$TAIL_LINES  IncludeEnv=$INCLUDE_ENV"
step "实例列表：$INSTANCES_LIST"
step "输出根：$OUT_BASE"

# ----------------------------------------------------------------------------
# 5. 单实例路径解析
# ----------------------------------------------------------------------------

# 给定 instance_id，按 env.example 约定推导该实例的所有 per-instance 路径并 export
# 到当前 shell（供 _dump_one_instance 用）。读取顺序：
#   1) ~/.chatcopilot-<id>.env（推荐，包含机密）
#   2) 当前 shell 已有 env 变量（兜底）
# 注意：本函数会**覆盖**当前 shell 的 CHATCOPILOT_* 变量，调用方循环里下一个实例
# 会用新值覆盖前一个，不残留。
resolve_instance_paths() {
    local instance="$1"
    export CHATCOPILOT_INSTANCE_ID="$instance"
    local env_file="$HOME/.chatcopilot-${instance}.env"
    if [ ! -r "$env_file" ] && [ "$instance" = "default" ]; then
        # legacy 单实例：~/.chatcopilot.env
        env_file="$HOME/.chatcopilot.env"
    fi
    if [ -r "$env_file" ]; then
        # 只抽 export 行进 tmp 文件后 source，避免 ~/.bashrc 类的 side effect
        local tmp
        tmp="$(mktemp)"
        grep -E '^[[:space:]]*export[[:space:]]+[A-Za-z_][A-Za-z0-9_]*=' "$env_file" > "$tmp" 2>/dev/null || true
        # shellcheck disable=SC1090
        set -a; source "$tmp" 2>/dev/null || true; set +a
        rm -f "$tmp"
    fi

    # env.example 标准路径（per-instance）
    INST_ENV_FILE="$env_file"
    INST_MT_HOME="${CHATCOPILOT_HOME:-$HOME/ChatCopilot-${instance}}"
    [ -d "$INST_MT_HOME" ] || INST_MT_HOME="$HOME/ChatCopilot"  # 兜底：单仓库布局
    INST_WS_ROOT="${WORKSPACE_ROOT:-${CHATCOPILOT_WORKSPACE_ROOT:-$HOME/chatcopilot-workspaces/${instance}}}"
    INST_LOG_DIR="${CHATCOPILOT_LOG_DIR:-$HOME/chatcopilot-logs/${instance}}"
    INST_CC_HOME="${CHATCOPILOT_CC_HOME:-$HOME/.chatcopilot-runtime/${instance}}"
    INST_CC_CONFIG_DIR="${CHATCOPILOT_CC_CONNECT_CONFIG_DIR:-$INST_CC_HOME/.cc-connect}"
    INST_CC_CONF="$INST_CC_CONFIG_DIR/config.toml"
    INST_PY="${CHATCOPILOT_ACP_PY:-$INST_MT_HOME/.venv/bin/python}"
    INST_BOT_SPEC="${CHATCOPILOT_BOT_SPEC:-$INST_MT_HOME/bots/${instance}/bot.yaml}"
    INST_LLM_CHAT_ENV_PREFIX="$(ccp_bot_chat_env_prefix "$INST_BOT_SPEC")"
    INST_LLM_CHAT_API_KEY=""
    INST_LLM_CHAT_BASE_URL=""
    INST_LLM_CHAT_MODEL=""
    if [[ "$INST_LLM_CHAT_ENV_PREFIX" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        local api_key_var="${INST_LLM_CHAT_ENV_PREFIX}_API_KEY"
        local base_url_var="${INST_LLM_CHAT_ENV_PREFIX}_BASE_URL"
        local model_var="${INST_LLM_CHAT_ENV_PREFIX}_MODEL"
        INST_LLM_CHAT_API_KEY="${!api_key_var:-}"
        INST_LLM_CHAT_BASE_URL="${!base_url_var:-}"
        INST_LLM_CHAT_MODEL="${!model_var:-}"
    fi
}

# ----------------------------------------------------------------------------
# 6. Runtime metadata 采集（Phase 3）
# ----------------------------------------------------------------------------

# 找本实例运行中的 cc-connect PID（按 environ HOME=$CC_HOME 严格隔离），与 status.sh
# 同款逻辑。多实例并行时不会把别实例的 PID 算进来。
list_instance_pids() {
    local cc_home="$1"
    local pidfile="$cc_home/cc-connect.pid"
    local pids=""
    if [ -r "$pidfile" ]; then
        local p
        p="$(tr -d ' \t\r\n' < "$pidfile" 2>/dev/null || true)"
        if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
            pids="$p"
        fi
    fi
    local pid environ
    while IFS= read -r pid; do
        [ -z "$pid" ] && continue
        [ -r "/proc/$pid/environ" ] || continue
        environ="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null || true)"
        if echo "$environ" | grep -Fxq "HOME=$cc_home"; then
            if ! echo " $pids " | grep -Fq " $pid "; then
                pids="${pids:+$pids }$pid"
            fi
        fi
    done < <(pgrep -x cc-connect 2>/dev/null || pgrep -f cc-connect 2>/dev/null || true)
    echo "$pids"
}

render_versions_txt() {
    local out="$1"
    {
        echo "# generated_at: $(date -Iseconds)"
        echo "# CHATCOPILOT_INSTANCE_ID=${CHATCOPILOT_INSTANCE_ID:-}"
        echo
        echo "## git"
        if [ -d "$INST_MT_HOME/.git" ]; then
            (
                cd "$INST_MT_HOME" || exit 0
                echo "git_rev=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
                echo "git_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
                local porcelain
                porcelain="$(git status --porcelain 2>/dev/null || true)"
                if [ -n "$porcelain" ]; then
                    echo "git_dirty=yes"
                    echo "git_status_porcelain<<<"
                    echo "$porcelain"
                    echo ">>>"
                else
                    echo "git_dirty=no"
                fi
                echo "git_last_commit<<<"
                git log -1 --pretty=format:'%H%n%an <%ae>%n%ai%n%s' 2>/dev/null || echo "(no commits)"
                echo
                echo ">>>"
            )
        else
            echo "git=N/A（$INST_MT_HOME 不是 git 仓库）"
        fi

        echo
        echo "## python"
        echo "python_path=$INST_PY"
        if [ -x "$INST_PY" ]; then
            "$INST_PY" --version 2>&1 | sed 's/^/python_version=/'
            echo "pip_freeze<<<"
            "$INST_PY" -m pip freeze 2>/dev/null \
                | grep -iE '^(chatcopilot|agent-client-protocol|acp|litellm|openai|mcp|lark|pydantic|httpx|pyyaml|click)' || true
            echo ">>>"
        else
            echo "python_version=(unavailable, $INST_PY not executable)"
        fi

        echo
        echo "## node / cc-connect"
        command -v node >/dev/null 2>&1 && echo "node_version=$(node --version 2>&1)" || echo "node=N/A"
        command -v npm  >/dev/null 2>&1 && echo "npm_version=$(npm --version 2>&1)" || echo "npm=N/A"
        if command -v cc-connect >/dev/null 2>&1; then
            echo "cc_connect_path=$(command -v cc-connect)"
            echo "cc_connect_version=$(cc-connect --version 2>&1 | head -1)"
        else
            echo "cc_connect=N/A（PATH 里没有）"
        fi
        if command -v lark-cli >/dev/null 2>&1; then
            echo "lark_cli_version=$(lark-cli --version 2>&1 | head -1)"
        fi
    } > "$out" 2>&1
}

render_system_txt() {
    local out="$1"
    {
        echo "# generated_at: $(date -Iseconds)"
        echo
        echo "## uname"
        uname -a 2>&1
        echo
        echo "## memory"
        free -h 2>&1
        echo
        echo "## disk"
        df -h "$HOME" "$INST_LOG_DIR" /tmp 2>/dev/null | sort -u
        echo
        echo "## wsl.conf"
        [ -r /etc/wsl.conf ] && cat /etc/wsl.conf || echo "(no /etc/wsl.conf)"
        echo
        echo "## env (non-secret)"
        echo "HOME=$HOME"
        echo "PATH=$PATH"
        echo "SHELL=${SHELL:-}"
        echo "LANG=${LANG:-}"
        echo "TZ=${TZ:-$(cat /etc/timezone 2>/dev/null || echo unknown)}"
        echo "USER=${USER:-$(id -un)}"
        echo
        echo "## chatcopilot env (non-secret subset)"
        env | grep -E '^CHATCOPILOT_' | grep -viE '(SECRET|TOKEN|API_KEY|PASSWORD)' | sort
    } > "$out" 2>&1
}

render_network_txt() {
    local out="$1"
    local pids="$2"
    {
        echo "# generated_at: $(date -Iseconds)"
        echo
        echo "## cc-connect sockets (ss -tulpn)"
        if [ -n "$pids" ]; then
            local pid_re
            pid_re="$(echo "$pids" | tr ' ' '|')"
            if command -v ss >/dev/null 2>&1; then
                ss -tulpn 2>/dev/null | awk -v pids="^pid=($pid_re)," 'NR==1 || $0 ~ pids' || true
                echo
                echo "## all established (egrep cc-connect)"
                ss -tnp 2>/dev/null | awk -v pids="pid=($pid_re)," 'NR==1 || $0 ~ pids' || true
            else
                echo "(ss 不可用)"
            fi
        else
            echo "(无 cc-connect PID，跳过)"
        fi

        echo
        echo "## Chat LLM gateway 探活"
        local base_url
        base_url="$INST_LLM_CHAT_BASE_URL"
        echo "env_prefix=${INST_LLM_CHAT_ENV_PREFIX:-(unresolved)}"
        echo "model=${INST_LLM_CHAT_MODEL:-(not configured)}"
        echo "credential=$([ -n "$INST_LLM_CHAT_API_KEY" ] && echo configured || echo missing)"
        echo "base_url=${base_url:-(not configured)}"
        if [ -z "$base_url" ]; then
            echo "probe: skipped (BASE_URL not configured)"
        elif command -v curl >/dev/null 2>&1; then
            local code_time
            code_time="$(curl --max-time 5 -sS -o /dev/null -w 'http_code=%{http_code} time_total=%{time_total}s\n' "$base_url" 2>&1)"
            echo "probe: $code_time"
        else
            echo "(curl 不可用)"
        fi

        echo
        echo "## DNS"
        local host
        host="$(echo "$base_url" | awk -F/ '{print $3}')"
        if [ -n "$host" ] && command -v getent >/dev/null 2>&1; then
            getent ahosts "$host" 2>&1 | head -10
        fi

        echo
        echo "## resolv.conf"
        [ -r /etc/resolv.conf ] && cat /etc/resolv.conf || echo "(no /etc/resolv.conf)"
    } > "$out" 2>&1
}

render_process_detail_txt() {
    local out="$1"
    local pids="$2"
    {
        echo "# generated_at: $(date -Iseconds)"
        if [ -z "$pids" ]; then
            echo "(本实例无运行中的 cc-connect 进程)"
            return
        fi
        for pid in $pids; do
            echo
            echo "================================================================"
            echo "## PID $pid"
            echo "================================================================"
            if [ -r "/proc/$pid/cmdline" ]; then
                echo "cmdline: $(tr '\0' ' ' < /proc/$pid/cmdline)"
            fi
            echo
            echo "### /proc/$pid/status"
            if [ -r "/proc/$pid/status" ]; then
                grep -E '^(Name|State|Pid|PPid|Threads|VmRSS|VmSize|VmPeak|FDSize|voluntary_ctxt_switches|nonvoluntary_ctxt_switches):' \
                    "/proc/$pid/status" 2>/dev/null
            fi
            echo
            echo "### ps -p $pid -o pid,ppid,etime,pcpu,pmem,nlwp,stat,args"
            ps -p "$pid" -o pid,ppid,etime,pcpu,pmem,nlwp,stat,args 2>/dev/null || true
            echo
            echo "### lsof -p $pid (top 200, may need root for full view)"
            if command -v lsof >/dev/null 2>&1; then
                lsof -p "$pid" 2>/dev/null | head -200 || echo "(lsof empty / not permitted)"
            else
                echo "(lsof 不可用)"
            fi
        done
    } > "$out" 2>&1
}

# 时间轴聚合：把 cc-connect.log + runtime/<date>.log + questions/<date>.log 抽出
# <ts>\t<source>\t<level>\t<message> 合并按 ts 排序，便于回答"14:30-14:35 之间发生了什么"。
# 三种 ts 前缀的解析：
#   - cc-connect.log：通常是 `[YYYY-MM-DD HH:MM:SS]` 或 `YYYY-MM-DDTHH:MM:SS.fffZ`
#   - runtime/<date>.log：`[YYYY-MM-DD HH:MM:SS,fff] LEVEL name | msg`
#   - questions/<date>.log：`[YYYY-MM-DD HH:MM:SS+08:00] | user | msg`
render_timeline_tsv() {
    local out="$1"
    local cc_log="$2"
    local runtime_log="$3"
    local question_log="$4"
    awk -v ccfile="$cc_log" -v rtfile="$runtime_log" -v qfile="$question_log" '
        BEGIN {
            FS = "";
            ts_re = "^\\[?([0-9]{4}-[0-9]{2}-[0-9]{2})[T ]([0-9]{2}:[0-9]{2}:[0-9]{2})";
        }
        function extract_ts(line,    arr) {
            if (match(line, /^\[?([0-9]{4}-[0-9]{2}-[0-9]{2})[T ]([0-9]{2}:[0-9]{2}:[0-9]{2})/, arr)) {
                return arr[1] " " arr[2];
            }
            return "";
        }
        function emit(file, source, line,    ts, level, msg) {
            ts = extract_ts(line);
            if (ts == "") return;
            level = "INFO";
            if (line ~ /\bERROR\b/ || line ~ /\bERR\b/ || line ~ /\[ERR\]/) level = "ERROR";
            else if (line ~ /\bWARN\b/ || line ~ /\bWARNING\b/) level = "WARN";
            else if (line ~ /\bDEBUG\b/) level = "DEBUG";
            msg = line;
            gsub(/\t/, " ", msg);
            print ts "\t" source "\t" level "\t" msg;
        }
        FNR == 1 {
            if (FILENAME == ccfile) current_source = "cc-connect";
            else if (FILENAME == rtfile) current_source = "runtime";
            else if (FILENAME == qfile) current_source = "question";
            else current_source = "unknown";
        }
        { emit(FILENAME, current_source, $0); }
    ' "$cc_log" "$runtime_log" "$question_log" 2>/dev/null \
        | sort -k1,2 \
        | { printf "ts\tsource\tlevel\tmessage\n"; cat; } \
        > "$out"
}

# 生成 attachments_manifest.json：扫 WS_ROOT 下所有 user 目录的 attachments/downloads/
# results/uploads，按文件级输出 {path: {size, mtime_iso, sha256, kind}}。
# 不拷文件内容（用户要求 "log 只存 string 描述"）。
render_attachments_manifest() {
    local out="$1"
    {
        echo "{"
        echo "  \"generated_at\": \"$(date -Iseconds)\","
        echo "  \"ws_root\": \"$INST_WS_ROOT\","
        echo "  \"files\": {"
        local first=1
        if [ -d "$INST_WS_ROOT" ]; then
            while IFS= read -r f; do
                [ -f "$f" ] || continue
                local rel size mtime sha
                rel="${f#"$INST_WS_ROOT"/}"
                size="$(stat -c %s "$f" 2>/dev/null || echo 0)"
                mtime="$(date -d "@$(stat -c %Y "$f" 2>/dev/null || echo 0)" -Iseconds 2>/dev/null || echo "")"
                sha="$(sha256sum "$f" 2>/dev/null | awk '{print $1}')"
                local kind="unknown"
                case "$rel" in
                    */attachments/*) kind=attachments ;;
                    */downloads/*)   kind=downloads ;;
                    */results/*)     kind=results ;;
                    */uploads/*)     kind=uploads ;;
                esac
                if [ "$first" = 1 ]; then
                    first=0
                else
                    printf ',\n'
                fi
                # JSON 编码 rel（仅可能出现的特殊字符是 \ 与 "）
                local esc
                esc="${rel//\\/\\\\}"
                esc="${esc//\"/\\\"}"
                printf '    "%s": {"size": %s, "mtime_iso": "%s", "sha256": "%s", "kind": "%s"}' \
                    "$esc" "$size" "$mtime" "$sha" "$kind"
            done < <(find "$INST_WS_ROOT" -type f \( \
                -path '*/attachments/*' -o -path '*/downloads/*' \
                -o -path '*/results/*' -o -path '*/uploads/*' \) 2>/dev/null)
        fi
        echo
        echo "  }"
        echo "}"
    } > "$out"
}

# 生成脱敏 env 副本：保留 KEY 名，值 redact 为 ***<len>chars。
# 用于事后无顾虑分享给同事看"哪些 KEY 设了/没设"。
render_env_sanitized() {
    local src="$1"
    local dst="$2"
    [ -r "$src" ] || return
    awk '
        /^[[:space:]]*#/ { print; next }
        /^[[:space:]]*$/ { print; next }
        /^[[:space:]]*export[[:space:]]+[A-Za-z_][A-Za-z0-9_]*=/ {
            n = split($0, a, "=");
            key_part = a[1];
            # 拼回值（应对值里含 = 的情况）
            val = "";
            for (i = 2; i <= n; i++) { val = (i == 2 ? a[i] : val "=" a[i]); }
            # 去包裹引号
            gsub(/^"/, "", val); gsub(/"$/, "", val);
            gsub(/^\x27/, "", val); gsub(/\x27$/, "", val);
            printf "%s=\"***%dchars\"\n", key_part, length(val);
            next
        }
        { print }
    ' "$src" > "$dst"
}

# ----------------------------------------------------------------------------
# 7. 单实例 dump 主流程
# ----------------------------------------------------------------------------
dump_one_instance() {
    local instance="$1"
    local inst_out="$2"
    local mem_count=0
    local tr_count=0
    local qcount=0

    resolve_instance_paths "$instance"

    step "[$instance] 实例路径："
    dim "MT_HOME = $INST_MT_HOME"
    dim "WS_ROOT = $INST_WS_ROOT"
    dim "LOG_DIR = $INST_LOG_DIR"
    dim "CC_HOME = $INST_CC_HOME    CC_CONFIG = $INST_CC_CONF"
    dim "ENV_FILE = $INST_ENV_FILE  (include=$INCLUDE_ENV)"

    # ------- DRY-RUN 列清单后返回 -------
    if [ "$DRY_RUN" = 1 ]; then
        step "[$instance] dry-run 清单："
        echo "  configs/cc-connect.config.toml   <- $INST_CC_CONF"
        [ "$INCLUDE_ENV" = 1 ] && echo "  configs/.chatcopilot.env         <- $INST_ENV_FILE (+ .sanitized)"
        echo "  logs/cc-connect/<date>.log       <- $INST_LOG_DIR/cc-connect/"
        echo "  logs/runtime/<date>.log          <- $INST_LOG_DIR/runtime/"
        echo "  logs/questions/<date>.log        <- $INST_LOG_DIR/"
        echo "  logs/raw/<date>.log              <- $INST_LOG_DIR/raw/"
        echo "  runtime/{status,processes,versions,system,network,process_detail,attachments_manifest,timeline}.*"
        if [ "$MODE" = "full" ]; then
            [ -d "$INST_WS_ROOT" ] && find "$INST_WS_ROOT" -maxdepth 6 -name "MEMORY.md" 2>/dev/null | while IFS= read -r f; do
                echo "  memories/...   <- $f"
            done
            [ -d "$INST_WS_ROOT" ] && find "$INST_WS_ROOT" -maxdepth 6 -name "*.jsonl" -path "*/transcripts/*" 2>/dev/null | while IFS= read -r f; do
                echo "  transcripts/.. <- $f"
            done
            echo "  prompts/persona_source_*.py.snapshot"
            echo "  prompts/system_prompt_*.md  (由 venv python 现场渲染)"
            echo "  runtime/tools_schema.json"
        fi
        return
    fi

    # ------- 建子目录 -------
    mkdir -p "$inst_out"/{configs,logs/cc-connect,logs/runtime,logs/questions,logs/raw,runtime} \
        || { err "无法创建 $inst_out"; return 1; }
    if [ "$MODE" = "full" ]; then
        mkdir -p "$inst_out"/{memories,transcripts,prompts}
    fi

    # ------- configs -------
    if [ -f "$INST_CC_CONF" ]; then
        cp "$INST_CC_CONF" "$inst_out/configs/cc-connect.config.toml"
        ok "[$instance] configs/cc-connect.config.toml"
    else
        warn "[$instance] cc-connect config 缺失：$INST_CC_CONF"
    fi
    if [ -f "$INST_BOT_SPEC" ]; then
        cp "$INST_BOT_SPEC" "$inst_out/configs/bot.yaml"
        ok "[$instance] configs/bot.yaml"
    fi
    if [ "$INCLUDE_ENV" = 1 ] && [ -r "$INST_ENV_FILE" ]; then
        cp "$INST_ENV_FILE" "$inst_out/configs/.chatcopilot.env"
        chmod 600 "$inst_out/configs/.chatcopilot.env" 2>/dev/null || true
        render_env_sanitized "$INST_ENV_FILE" "$inst_out/configs/.chatcopilot.env.sanitized"
        warn "[$instance] configs/.chatcopilot.env （含机密！.gitignore 已保护，勿手动 commit）"
        ok "[$instance] configs/.chatcopilot.env.sanitized （脱敏副本，可放心分享）"
    fi

    # ------- logs：cc-connect / runtime / questions / raw / _hook_errors -------
    copy_logs_dir() {
        local src_dir="$1"
        local dst_dir="$2"
        [ -d "$src_dir" ] || return
        while IFS= read -r f; do
            [ -f "$f" ] || continue
            local base
            base="$(basename "$f")"
            if [ "$TAIL_LINES" -gt 0 ]; then
                tail -n "$TAIL_LINES" "$f" > "$dst_dir/$base" 2>/dev/null || true
            else
                cp "$f" "$dst_dir/$base"
            fi
        done < <(find "$src_dir" -maxdepth 1 -type f \( -name "*.log" -o -name "current.log" \) 2>/dev/null)
    }

    # 新位置：$LOG_DIR/cc-connect/ （start.sh 改造后）
    copy_logs_dir "$INST_LOG_DIR/cc-connect" "$inst_out/logs/cc-connect"
    # 兼容老 /tmp/cc-connect.log：如果它不是 symlink（旧实例 / 老脚本写的），单独拷一份兜底
    if [ -e /tmp/cc-connect.log ] && [ ! -L /tmp/cc-connect.log ]; then
        if [ "$TAIL_LINES" -gt 0 ]; then
            tail -n "$TAIL_LINES" /tmp/cc-connect.log > "$inst_out/logs/cc-connect/_tmp_cc-connect.log" 2>/dev/null || true
        else
            cp /tmp/cc-connect.log "$inst_out/logs/cc-connect/_tmp_cc-connect.log" 2>/dev/null || true
        fi
    fi

    # runtime/<date>.log（FileHandler 写的本项目 chatcopilot.* logger）
    copy_logs_dir "$INST_LOG_DIR/runtime" "$inst_out/logs/runtime"

    # questions/<date>.log + raw/<date>.log + _hook_errors.log
    if [ -d "$INST_LOG_DIR" ]; then
        while IFS= read -r f; do
            [ -f "$f" ] || continue
            cp "$f" "$inst_out/logs/questions/$(basename "$f")"
            qcount=$((qcount + 1))
        done < <(find "$INST_LOG_DIR" -maxdepth 1 -type f -name "*.log" -not -name "_hook_errors.log" 2>/dev/null)
        [ "$qcount" -gt 0 ] && ok "[$instance] logs/questions/ ($qcount 个日期文件)"

        if [ -d "$INST_LOG_DIR/raw" ]; then
            while IFS= read -r f; do
                [ -f "$f" ] || continue
                cp "$f" "$inst_out/logs/raw/$(basename "$f")"
            done < <(find "$INST_LOG_DIR/raw" -maxdepth 1 -type f -name "*.log" 2>/dev/null)
        fi
        if [ -f "$INST_LOG_DIR/_hook_errors.log" ]; then
            cp "$INST_LOG_DIR/_hook_errors.log" "$inst_out/logs/_hook_errors.log"
        fi
    fi

    # ------- memories / transcripts (full only, 按 user_id 过滤) -------
    if [ "$MODE" = "full" ] && [ -d "$INST_WS_ROOT" ]; then
        while IFS= read -r mem; do
            [ -f "$mem" ] || continue
            local rel="${mem#"$INST_WS_ROOT"/}"
            if [ -n "$USERS_PIPE" ]; then
                local hit=0
                for uid in $(echo "$USERS_FILTER" | tr ',' ' '); do
                    case "$rel" in
                        *p2p_${uid}/*|*p2p_${uid}|*user_${uid}/*|*user_${uid}) hit=1; break ;;
                    esac
                done
                [ "$hit" = 1 ] || continue
            fi
            local dest="$inst_out/memories/$rel"
            mkdir -p "$(dirname "$dest")"
            cp "$mem" "$dest"
            mem_count=$((mem_count + 1))
        done < <(find "$INST_WS_ROOT" -maxdepth 6 -name "MEMORY.md" 2>/dev/null)

        while IFS= read -r tr_f; do
            [ -f "$tr_f" ] || continue
            local rel="${tr_f#"$INST_WS_ROOT"/}"
            if [ -n "$USERS_PIPE" ]; then
                local hit=0
                for uid in $(echo "$USERS_FILTER" | tr ',' ' '); do
                    case "$rel" in
                        *p2p_${uid}/*|*user_${uid}/*) hit=1; break ;;
                    esac
                done
                [ "$hit" = 1 ] || continue
            fi
            local dest="$inst_out/transcripts/$rel"
            mkdir -p "$(dirname "$dest")"
            cp "$tr_f" "$dest"
            tr_count=$((tr_count + 1))
        done < <(find "$INST_WS_ROOT" -maxdepth 6 -name "*.jsonl" -path "*/transcripts/*" 2>/dev/null)

        ok "[$instance] memories: $mem_count / transcripts: $tr_count"
    fi

    # ------- prompts (full only) -------
    if [ "$MODE" = "full" ]; then
        for p in feishu qq; do
            local src="$INST_MT_HOME/src/chatcopilot/platforms/$p/persona.py"
            [ -f "$src" ] && cp "$src" "$inst_out/prompts/persona_source_${p}.py.snapshot"
        done
        if [ -x "$INST_PY" ]; then
            "$INST_PY" - "$inst_out/prompts" "$INST_WS_ROOT" <<'PYEOF' 2>>"$inst_out/runtime/render_prompt.err" || true
import sys, os
from pathlib import Path

out_dir = Path(sys.argv[1])
ws_root = sys.argv[2]
sys.path.insert(0, str(Path(os.environ.get("CHATCOPILOT_HOME", str(Path.home() / "ChatCopilot"))) / "src"))

from chatcopilot.botspec import load_runtime_context
from chatcopilot.middleware.access_control import AssistantMode, Role
from chatcopilot.middleware.runtime.workspace import Workspace
from chatcopilot.platforms import router as _router

runtime = load_runtime_context()
build_system_prompt = _router.get_persona_builder(runtime.platform_type)

fake = Workspace(
    root=Path(ws_root) / "_dump_render_only",
    chat_kind="p2p",
    chat_id=None,
    user_id="ou_dump_render_user",
    user_name="(dump 渲染占位)",
)

if _router.supports_role_matrix(runtime.platform_type):
    roles = [(Role.OWNER, AssistantMode.GENERAL), (Role.ADMIN, AssistantMode.PERFORMANCE), (Role.USER, AssistantMode.PERFORMANCE)]
    for role, mode in roles:
        text = build_system_prompt(
            fake,
            role=role,
            assistant_mode=mode,
            bot_system_prompt=runtime.system_prompt,
            bot_refusal_prompt=runtime.refusal_prompt,
            capability_prompt_fragments=runtime.capability_prompt_fragments,
            skill_index=runtime.skills,
            mode_prompts=runtime.mode_prompt_overrides,
            role_prompts=runtime.role_prompt_overrides,
            safety_prompt=runtime.safety_prompt_override,
            memory_prompt=runtime.memory_prompt_override,
        )
        (out_dir / f"system_prompt_{role.value}.md").write_text(text, encoding="utf-8")
else:
    text = build_system_prompt(
        fake,
        bot_system_prompt=runtime.system_prompt,
        bot_refusal_prompt=runtime.refusal_prompt,
        capability_prompt_fragments=runtime.capability_prompt_fragments,
        skill_index=runtime.skills,
        mode_prompts=runtime.mode_prompt_overrides,
        role_prompts=runtime.role_prompt_overrides,
        safety_prompt=runtime.safety_prompt_override,
        memory_prompt=runtime.memory_prompt_override,
    )
    (out_dir / "system_prompt_default.md").write_text(text, encoding="utf-8")
PYEOF
            ok "[$instance] prompts/"
        else
            warn "[$instance] venv python 不可执行：$INST_PY；prompts 渲染跳过"
        fi
    fi

    # ------- runtime: status / processes -------
    bash "$(dirname "$0")/status.sh" > "$inst_out/runtime/status.txt" 2>&1 || true
    {
        echo "=== pgrep -af cc-connect ==="
        pgrep -af cc-connect 2>/dev/null || echo "(无 cc-connect 进程)"
        echo
        echo "=== ps -eo pid,etime,cmd | head -50 ==="
        ps -eo pid,etime,cmd 2>/dev/null | head -50
    } > "$inst_out/runtime/processes.txt"

    # ------- runtime: versions / system / network / process_detail / attachments_manifest -------
    local pids
    pids="$(list_instance_pids "$INST_CC_HOME")"
    render_versions_txt        "$inst_out/runtime/versions.txt"
    render_system_txt          "$inst_out/runtime/system.txt"
    render_network_txt         "$inst_out/runtime/network.txt"         "$pids"
    render_process_detail_txt  "$inst_out/runtime/process_detail.txt"  "$pids"
    render_attachments_manifest "$inst_out/runtime/attachments_manifest.json"
    ok "[$instance] runtime/{status,processes,versions,system,network,process_detail,attachments_manifest}.*"

    # ------- runtime: tools_schema (full only) -------
    if [ "$MODE" = "full" ] && [ -x "$INST_PY" ]; then
        "$INST_PY" - "$inst_out/runtime/tools_schema.json" <<'PYEOF' 2>>"$inst_out/runtime/render_tools.err" || true
import sys, os, json
from pathlib import Path
sys.path.insert(0, str(Path(os.environ.get("CHATCOPILOT_HOME", str(Path.home() / "ChatCopilot"))) / "src"))
from chatcopilot.agent.tools.registry import build_tools_schema
from chatcopilot.botspec import load_runtime_context
runtime = load_runtime_context()
tool_packs = getattr(runtime, "tool_packs", getattr(runtime, "capability_include", ()))
try:
    schema, _index = build_tools_schema(
        tool_packs=tool_packs,
        exclude_tools=runtime.exclude_tools,
    )
except TypeError:
    schema, _index = build_tools_schema(
        capabilities=tool_packs,
        exclude_tools=runtime.exclude_tools,
    )
Path(sys.argv[1]).write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
PYEOF
    fi

    # ------- runtime: timeline.tsv -------
    local cc_log_pick="" runtime_log_pick="" question_log_pick=""
    if [ -d "$inst_out/logs/cc-connect" ]; then
        cc_log_pick="$(find "$inst_out/logs/cc-connect" -maxdepth 1 -type f -name "*.log" | sort | tail -1)"
    fi
    if [ -d "$inst_out/logs/runtime" ]; then
        runtime_log_pick="$(find "$inst_out/logs/runtime" -maxdepth 1 -type f -name "*.log" | sort | tail -1)"
    fi
    if [ -d "$inst_out/logs/questions" ]; then
        question_log_pick="$(find "$inst_out/logs/questions" -maxdepth 1 -type f -name "*.log" | sort | tail -1)"
    fi
    render_timeline_tsv \
        "$inst_out/runtime/timeline.tsv" \
        "${cc_log_pick:-/dev/null}" \
        "${runtime_log_pick:-/dev/null}" \
        "${question_log_pick:-/dev/null}"
    ok "[$instance] runtime/timeline.tsv （cc-connect + runtime + question 合并按 ts）"

    # ------- 清理空 .err -------
    for e in "$inst_out"/runtime/*.err; do
        [ -f "$e" ] || continue
        [ -s "$e" ] || rm -f "$e"
    done

    # ------- per-instance manifest + README + .gitignore -------
    echo "*" > "$inst_out/.gitignore"
    cat > "$inst_out/README.md" <<EOF
# WSL 调试快照 — instance=$instance ($TS)

mode=$MODE  tail_lines=$TAIL_LINES  include_env=$INCLUDE_ENV  users_filter=${USERS_FILTER:-（全部）}

| 子目录 | 内容 |
|---|---|
| configs/ | cc-connect.config.toml + bot.yaml + .chatcopilot.env (+ sanitized) + 运行中的 sess env |
| memories/ | per-user MEMORY.md（按 \$WS_ROOT 下相对路径保留） |
| transcripts/ | per-session JSONL 对话历史（每行一条 LLM/工具消息） |
| prompts/ | persona.py 副本 + 最终 system prompt |
| logs/cc-connect/ | cc-connect 主日志（新位置 \$LOG_DIR/cc-connect/<date>.log） |
| logs/runtime/ | ACP runtime 的 chatcopilot.* logger 独立文件 |
| logs/questions/ | 用户提问精简日志（按日期） |
| logs/raw/ | 用户提问 raw 上下文（CC_HOOK_* 全量） |
| runtime/status.txt | bash status.sh 输出 |
| runtime/processes.txt | pgrep -af cc-connect + ps |
| runtime/tools_schema.json | full 模式才生成；当前注册的 tool schema |
| runtime/attachments_manifest.json | 用户附件文件级清单（不含内容） |
| runtime/versions.txt | git rev / dirty / pip freeze / cc-connect / node |
| runtime/system.txt | uname / free / df / wsl.conf / PATH |
| runtime/network.txt | ss -tulpn / Chat LLM gateway 探活 / DNS |
| runtime/process_detail.txt | /proc/<pid>/status + lsof + cmdline |
| runtime/timeline.tsv | cc-connect + runtime + question 三流合并按 ts 排序 |

| 元信息 | 值 |
|---|---|
| 生成时间 | $(date -Iseconds) |
| WSL 主机 | $(hostname) |
| instance_id | $instance |
| MT_HOME | $INST_MT_HOME |
| WS_ROOT | $INST_WS_ROOT |
| LOG_DIR | $INST_LOG_DIR |
| CC_HOME | $INST_CC_HOME |
| 包含机密 | $([ "$INCLUDE_ENV" = 1 ] && echo "是（.env 原文）+ .sanitized" || echo "否") |
| 附件 | 仅清单（attachments_manifest.json），不含文件本身 |
EOF

    {
        echo "{"
        echo "  \"instance_id\": \"$instance\","
        echo "  \"generated_at\": \"$(date -Iseconds)\","
        echo "  \"mode\": \"$MODE\","
        echo "  \"tail_lines\": $TAIL_LINES,"
        echo "  \"include_env\": $INCLUDE_ENV,"
        echo "  \"users_filter\": \"${USERS_FILTER}\","
        echo "  \"files\": ["
        local first=1
        while IFS= read -r f; do
            local rel size mtime sha
            rel="${f#"$inst_out"/}"
            [ "$rel" = "manifest.json" ] && continue
            size=$(stat -c %s "$f" 2>/dev/null || echo 0)
            mtime=$(stat -c %Y "$f" 2>/dev/null || echo 0)
            sha=$(sha256sum "$f" 2>/dev/null | awk '{print $1}')
            if [ "$first" = 1 ]; then first=0; else printf ',\n'; fi
            printf '    {"path": "%s", "size": %s, "mtime": %s, "sha256": "%s"}' "$rel" "$size" "$mtime" "$sha"
        done < <(find "$inst_out" -type f | sort)
        echo
        echo "  ]"
        echo "}"
    } > "$inst_out/manifest.json"
    ok "[$instance] manifest.json / README.md / .gitignore"
}

# ----------------------------------------------------------------------------
# 8. 主循环
# ----------------------------------------------------------------------------

# DRY-RUN：先不建目录，逐实例打印清单后退出
if [ "$DRY_RUN" = 1 ]; then
    for inst in $INSTANCES_LIST; do
        dump_one_instance "$inst" "$OUT_BASE/$inst"
    done
    echo
    ok "[dry-run] 不会落任何文件。"
    exit 0
fi

mkdir -p "$OUT_BASE" || { err "无法创建输出根：$OUT_BASE"; exit 1; }
echo "*" > "$OUT_BASE/.gitignore"

for inst in $INSTANCES_LIST; do
    echo
    dump_one_instance "$inst" "$OUT_BASE/$inst"
done

# ----------------------------------------------------------------------------
# 9. 全局 _meta.json
# ----------------------------------------------------------------------------
{
    echo "{"
    echo "  \"generated_at\": \"$(date -Iseconds)\","
    echo "  \"hostname\": \"$(hostname)\","
    echo "  \"wsl_user\": \"$(id -un)\","
    echo "  \"mode\": \"$MODE\","
    echo "  \"tail_lines\": $TAIL_LINES,"
    echo "  \"include_env\": $INCLUDE_ENV,"
    echo "  \"users_filter\": \"${USERS_FILTER}\","
    echo "  \"all_running\": $ALL_RUNNING,"
    echo "  \"instances\": ["
    _first=1
    for inst in $INSTANCES_LIST; do
        [ "$_first" = 1 ] && _first=0 || printf ',\n'
        printf '    "%s"' "$inst"
    done
    echo
    echo "  ]"
    echo "}"
} > "$OUT_BASE/_meta.json"

# ----------------------------------------------------------------------------
# 10. 可选 tar.gz
# ----------------------------------------------------------------------------
if [ "$ARCHIVE" = 1 ]; then
    step "打包 tar.gz"
    parent="$(dirname "$OUT_BASE")"
    base="$(basename "$OUT_BASE")"
    tarball="$parent/${base}.tar.gz"
    (cd "$parent" && tar czf "$tarball" "$base") && \
        ok "$tarball ($(stat -c %s "$tarball" | numfmt --to=iec --suffix=B 2>/dev/null || echo bytes))" || \
        err "tar 打包失败"
fi

# ----------------------------------------------------------------------------
# 11. 收尾
# ----------------------------------------------------------------------------
echo
ok "Dump 完成 → $OUT_BASE"
total_files=$(find "$OUT_BASE" -type f 2>/dev/null | wc -l)
total_size=$(du -sb "$OUT_BASE" 2>/dev/null | cut -f1)
total_size_h=$(numfmt --to=iec --suffix=B "$total_size" 2>/dev/null || echo "${total_size}B")
echo "  共 $total_files 个文件，$total_size_h"
case "$OUT_BASE" in
    /mnt/[a-z]/*)
        win_path=$(echo "$OUT_BASE" | sed -E 's|^/mnt/([a-z])/|\U\1:\\|; s|/|\\|g')
        echo
        echo "  在 Windows 资源管理器打开："
        echo "    explorer.exe $win_path"
        ;;
esac
