"""高层运维操作：状态 / 起停重启 / 同步 / 重建 / 诊断 / 任务列表 / 日志。

设计原则：
- 短操作（status / start / stop / restart / jobs）同步返回 dict。
- 长操作（sync / rebuild / dump）是生成器，逐行 yield 日志，便于后端做异步任务 + SSE。
- 所有对外返回都是 JSON-friendly 的基本类型，UI 永不接触脚本文本格式。
"""
from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from console.control import services, systemd
from console.control.discovery import repo_root
from console.control.instances import BotInstance
from console.control.observability import (
    KEEPALIVE,
    console_log_error,
    delete_task,
    follow_console_log,
    follow_log,
    follow_unit_log,
    jobs,
    resolve_log_files,
    tail_log,
    task_detail,
    task_events,
    task_flow,
    tasks,
)
from console.control.process_executor import run_capture
from console.control.yaml_io import load_yaml_mapping_or_empty
from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.provisioning import (
    ProvisioningError,
    build_provision_plan,
    patch_local_env,
    read_local_env_for_provision,
)
from chatcopilot.platforms.registry import get_adapter

__all__ = [
    "KEEPALIVE",
    "console_log_error",
    "delete_task",
    "follow_console_log",
    "follow_log",
    "follow_unit_log",
    "jobs",
    "resolve_log_files",
    "tail_log",
    "task_detail",
    "task_events",
    "task_flow",
    "tasks",
]

_CHANNEL_MARKERS = {
    "feishu": "connected to wss",
    "qq": "onebot.connected",
}
_XHS_MCP_REF = "xiaohongshu-search"
_XHS_MCP_CONTAINER = "chatcopilot-xiaohongshu-mcp"
_XHS_MCP_NOT_RUNNING_MESSAGE = "小红书 MCP 未启动，请先在服务管理中启动小红书 MCP。"


def _console_script(name: str) -> Path:
    """控制面动作脚本（console/scripts/<name>），按实例 id 操作宿主。"""
    return repo_root() / "console" / "scripts" / name


def _deploy_script(name: str) -> Path:
    return repo_root() / "deploy" / "wsl" / name


def _docker_service_script() -> Path:
    return repo_root() / "deploy" / "docker" / "services.sh"


# ---------------------------------------------------------------------------
# 更新控制台自身（重建前端 + 重启后端）
#
# 难点：脚本最后会 restart chatcopilot-console，即后端自己所属的服务。若脚本作为
# 后端进程的子进程运行，restart 时整个服务 cgroup 被杀，脚本会半途夭折。因此只允许
# systemd-run --user 创建独立 transient unit。setsid/nohup 不能脱离 systemd cgroup，
# 不具备这个安全属性，不能作为 fallback。这是 fire-and-forget：函数立即返回，真正的
# 构建 + 重启在 transient unit 中进行。
# ---------------------------------------------------------------------------
def trigger_console_update() -> Dict[str, object]:
    script = _deploy_script("deploy_console.sh")
    if not script.is_file():
        return {"ok": False, "error": f"找不到 {script}"}

    uid = os.getuid()
    runtime_dir = f"/run/user/{uid}"
    dbus = f"unix:path=/run/user/{uid}/bus"
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", dbus)

    unit = f"cc-console-update-{int(time.time())}"
    systemd_run = [
        "systemd-run",
        "--user",
        "--collect",
        f"--unit={unit}",
        "--property=Type=exec",
        f"--setenv=XDG_RUNTIME_DIR={runtime_dir}",
        f"--setenv=DBUS_SESSION_BUS_ADDRESS={dbus}",
        "bash",
        str(script),
        "--update-only",
    ]
    try:
        cp = subprocess.run(systemd_run, capture_output=True, text=True, env=env, timeout=20.0)
    except (OSError, subprocess.SubprocessError) as exc:
        cp = None
        run_err = str(exc)
    else:
        run_err = (cp.stderr or cp.stdout or "").strip()

    if cp is not None and cp.returncode == 0:
        return {
            "ok": True,
            "mode": "systemd-run",
            "unit": unit,
            "message": "控制台更新中（重建前端 + 重启后端），数十秒后请刷新页面。",
        }

    if not run_err and cp is not None:
        run_err = f"exit code {cp.returncode}"
    return {
        "ok": False,
        "error": (
            "无法通过独立 systemd transient unit 启动控制台更新；已安全中止，"
            "未运行更新脚本，也未获取 Evaluation maintenance lease。"
            "请在 WSL 终端执行 bash deploy/wsl/deploy_console.sh --update-only。"
            f"systemd-run: {run_err or 'unknown error'}"
        ),
    }


# ---------------------------------------------------------------------------
# 子进程流式执行
# ---------------------------------------------------------------------------
def run_streaming(
    args: List[str],
    *,
    cwd: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Iterator[str]:
    """逐行 yield stdout+stderr。最后 yield 一行形如 ``__EXIT__ <code>``。"""
    env = dict(os.environ)
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        yield f"[ERR] 无法启动命令 {args[0]}: {exc}"
        yield "__EXIT__ 127"
        return
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line.rstrip("\n")
    proc.wait()
    yield f"__EXIT__ {proc.returncode}"


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
def _read_tail_text(path: Path, *, max_bytes: int = 256 * 1024) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _log_error_lines(text: str) -> list[str]:
    return [
        ln
        for ln in text.splitlines()
        if ("level=ERROR" in ln or "panic:" in ln or "[ERR]" in ln)
    ]


def _summarize_error_lines(lines: list[str]) -> str:
    if not lines:
        return ""
    last = lines[-1]
    if "qq: ws read error" in last and "websocket: close 1000 (normal)" in last:
        return "QQ OneBot websocket is closing immediately; check the external NapCat provider."
    if "panic:" in last:
        return "Runtime panic detected in log tail."
    if "[ERR]" in last:
        return last.strip()[-240:]
    return last.strip()[-240:]


def _log_signal(inst: BotInstance) -> Dict[str, object]:
    runtime_log = inst.primary_log_file()
    info: Dict[str, object] = {
        "runtime_log": runtime_log,
        "runtime_log_age_s": None,
        "runtime_log_size": None,
        "channel_connected": None,
        "error_count": 0,
        "error_summary": "",
    }
    if runtime_log and Path(runtime_log).is_file():
        st = Path(runtime_log).stat()
        info["runtime_log_age_s"] = int(time.time() - st.st_mtime)
        info["runtime_log_size"] = st.st_size
        marker = _CHANNEL_MARKERS.get(inst.platform)
        text = _read_tail_text(Path(runtime_log))
        if marker:
            info["channel_connected"] = marker in text
        error_lines = _log_error_lines(text)
        info["error_count"] = len(error_lines)
        info["error_summary"] = _summarize_error_lines(error_lines)
    return info


def _gateway_main_process_matches(inst: BotInstance, pid: int) -> bool:
    if inst.runtime_kind != "gateway" or pid <= 0 or not inst.wsl_home:
        return False
    proc = Path("/proc") / str(pid)
    try:
        if proc.stat().st_uid != os.getuid():
            return False
        expected_python = (Path(inst.wsl_home) / ".venv" / "bin" / "python").resolve(strict=True)
        actual_python = (proc / "exe").resolve(strict=True)
        argv = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        environ = set((proc / "environ").read_bytes().rstrip(b"\0").split(b"\0"))
    except OSError:
        return False
    expected_bot = str(Path(inst.wsl_home) / "bots" / inst.instance_id / "bot.yaml").encode()
    return (
        actual_python == expected_python
        and argv[1:] == [b"-m", b"chatcopilot", b"run", b"--bot", expected_bot]
        and f"CHATCOPILOT_INSTANCE_ID={inst.instance_id}".encode() in environ
    )


def _mcp_services_status() -> list[dict[str, object]]:
    """Check Docker-based MCP services health via container inspect.

    Delegates to console.control.services for the actual inspect logic,
    then maps the result to the legacy McpServiceStatus shape expected
    by BotCard's MCP row (kept for backward compatibility).
    """
    results: list[dict[str, object]] = []
    for svc in services.SERVICES:
        if svc.service_type != "compose":
            continue
        st = services.compose_status(svc)
        results.append({
            "id": svc.id,
            "container": svc.container,
            "running": st["state"] in ("healthy", "running"),
            "health": st["state"],
            "color": st["color"],
        })
    return results


def _config_enabled(value: Any, *, default: bool) -> bool:
    """Parse recognized BotSpec bool values; invalid values fail closed."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return False


def _enabled_search_provider_kinds(bot_data: dict[str, Any]) -> set[str]:
    """Return explicitly enabled direct-provider kinds from one BotSpec mapping."""
    agents = bot_data.get("agents") if isinstance(bot_data.get("agents"), dict) else {}
    unified = (
        agents.get("unified_search")
        if isinstance(agents.get("unified_search"), dict)
        else {}
    )
    if not _config_enabled(unified.get("enabled"), default=False):
        return set()
    providers = unified.get("providers")
    if not isinstance(providers, list):
        return set()
    return {
        str(provider.get("kind") or "").strip().lower()
        for provider in providers
        if isinstance(provider, dict)
        and _config_enabled(provider.get("enabled"), default=True)
        if str(provider.get("kind") or "").strip()
    }


def bot_enabled_services(inst: BotInstance) -> list[dict[str, object]]:
    """Return services enabled by this bot's BotSpec and platform binding."""
    bot_data = _load_yaml_mapping(_bot_spec_path(inst))
    tool_packs = _bot_tool_pack_ids(bot_data)
    search_provider_kinds = _enabled_search_provider_kinds(bot_data)
    status_by_id = {str(item.get("id")): item for item in services.all_services_status()}
    enabled: list[dict[str, object]] = []

    for svc in services.SERVICES:
        reasons: list[str] = []
        matched = False

        matched_mcp_refs = [ref for ref in svc.mcp_refs if _bot_uses_mcp_ref(inst, ref)]
        if matched_mcp_refs:
            matched = True
            reasons.extend(f"MCP: {ref}" for ref in matched_mcp_refs)

        matched_provider_kinds = [
            kind for kind in svc.search_provider_kinds if kind in search_provider_kinds
        ]
        if matched_provider_kinds:
            matched = True
            reasons.extend(
                f"Search provider: {kind}" for kind in matched_provider_kinds
            )

        matched_tool_packs = [pack for pack in svc.tool_pack_ids if pack in tool_packs]
        if matched_tool_packs:
            matched = True
            reasons.extend(f"Tool pack: {pack}" for pack in matched_tool_packs)

        if inst.platform in svc.platforms:
            matched = True
            reasons.append(f"Platform: {inst.platform}")

        if inst.instance_id in svc.bound_instance_ids:
            matched = True
            reasons.append(f"Instance: {inst.instance_id}")

        if not matched:
            continue

        status_key = (
            f"{svc.id}:{inst.instance_id}"
            if svc.service_type == "standalone" and inst.instance_id in svc.bound_instance_ids
            else svc.id
        )
        status_item = status_by_id.get(status_key) or status_by_id.get(svc.id) or {}
        enabled.append({
            "id": status_key,
            "service_id": svc.id,
            "display_name": svc.display_name,
            "service_type": svc.service_type,
            "state": status_item.get("state", "unknown"),
            "color": status_item.get("color", "grey"),
            "container": status_item.get("container"),
            "uptime_s": status_item.get("uptime_s"),
            "reasons": reasons,
            "actions": list(svc.actions),
            "has_login": svc.has_login,
            "has_doctor": svc.has_doctor,
            "instance_id": status_item.get("instance_id"),
        })

    return enabled


def status(inst: BotInstance, *, include_services: bool = True) -> Dict[str, object]:
    sd_ok = systemd.is_available()
    props = systemd.show(inst.unit_short) if sd_ok else {}
    active_state = props.get("ActiveState", "unknown")
    main_pid = props.get("MainPID", "0")
    try:
        pid_int = int(main_pid)
    except ValueError:
        pid_int = 0
    gateway_process_verified = (
        _gateway_main_process_matches(inst, pid_int)
        if inst.runtime_kind == "gateway" and active_state == "active"
        else None
    )
    result: Dict[str, object] = {
        "instance_id": inst.instance_id,
        "display_name": inst.display_name,
        "platform": inst.platform,
        "runtime_kind": inst.runtime_kind,
        "is_deployed": inst.is_deployed,
        "unit": inst.unit_short,
        "systemd_available": sd_ok,
        "unit_installed": systemd.unit_installed() if sd_ok else False,
        "registered": instance_registered(inst),
        "active_state": active_state,
        "sub_state": props.get("SubState", ""),
        "enabled": props.get("UnitFileState", ""),
        "pid": pid_int or None,
        "since": props.get("ActiveEnterTimestamp", "") or None,
        "main_process_kind": "gateway" if inst.runtime_kind == "gateway" else "legacy_edge",
        "main_process_verified": gateway_process_verified,
        "running": active_state == "active" and pid_int > 0 and gateway_process_verified is not False,
    }
    result.update(_log_signal(inst))
    if include_services:
        result["mcp_services"] = _mcp_services_status()
        result["enabled_services"] = bot_enabled_services(inst)
    else:
        result["mcp_services"] = []
        result["enabled_services"] = []
    bot_data = _load_yaml_mapping(_bot_spec_path(inst))
    result["tool_packs"] = _bot_tool_packs_grouped(bot_data)
    result["checks"], result["reasons"] = _status_checks(result)
    return result


def _status_checks(status_data: Dict[str, object]) -> tuple[list[dict[str, object]], list[str]]:
    checks: list[dict[str, object]] = []
    reasons: list[str] = []

    def add(name: str, ok: bool, severity: str, message: str) -> None:
        checks.append({"name": name, "ok": ok, "severity": severity, "message": message})
        if not ok:
            reasons.append(message)

    add("deployed", bool(status_data.get("is_deployed")), "critical", "Instance files are not deployed.")
    add("registered", bool(status_data.get("registered")), "critical", "systemd unit is not registered.")
    add("running", bool(status_data.get("running")), "critical", "bot process is not running.")
    if status_data.get("runtime_kind") == "gateway":
        add(
            "gateway_main_process",
            status_data.get("main_process_verified") is True,
            "critical",
            "systemd MainPID is not the exact instance Gateway process.",
        )
    if status_data.get("running") and status_data.get("channel_connected") is False:
        add("channel_connection", False, "critical", "configured Channel is not connected.")
    elif status_data.get("running") and status_data.get("channel_connected") is True:
        add("channel_connection", True, "info", "configured Channel is connected.")
    runtime_age = status_data.get("runtime_log_age_s")
    if status_data.get("runtime_log_size") is not None:
        age_text = f" Last updated {int(float(runtime_age))}s ago." if runtime_age is not None else ""
        add("runtime_logs", True, "info", f"runtime file log is available.{age_text}")
    error_count = int(status_data.get("error_count") or 0)
    if error_count > 0:
        summary = str(status_data.get("error_summary") or "").strip()
        suffix = f": {summary}" if summary else "."
        add("log_errors", False, "warning", f"runtime log tail contains {error_count} error line(s){suffix}")
    return checks, reasons


# ---------------------------------------------------------------------------
# 注册态：模板已装 + 本实例 per-instance conf 存在（chatcopilot@.service 的
# EnvironmentFile=%h/.config/chatcopilot-console/%i.env），两者齐全才真正可重启。
# ---------------------------------------------------------------------------
def _console_conf_path(inst: BotInstance) -> Path:
    return Path.home() / ".config" / "chatcopilot-console" / f"{inst.instance_id}.env"


def instance_registered(inst: BotInstance) -> bool:
    return (
        systemd.is_available()
        and systemd.unit_installed()
        and _console_conf_path(inst).is_file()
    )


# ---------------------------------------------------------------------------
# 起停重启（短操作）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Shared Docker services: Xiaohongshu MCP login flow
# ---------------------------------------------------------------------------
def _xhs_mcp_url() -> str:
    return "http://localhost:18060/mcp"


def _is_xhs_mcp_connection_refused(exc: BaseException) -> bool:
    candidates: list[object] = [exc]
    reason = getattr(exc, "reason", None)
    if reason is not None:
        candidates.append(reason)
    for item in candidates:
        if isinstance(item, ConnectionRefusedError):
            return True
        if isinstance(item, OSError) and item.errno in {errno.ECONNREFUSED, 10061}:
            return True
        text = str(item).lower()
        if "connection refused" in text or "errno 111" in text or "winerror 10061" in text:
            return True
    return False


def _xhs_api_error_message(exc: BaseException) -> str:
    if _is_xhs_mcp_connection_refused(exc):
        return _XHS_MCP_NOT_RUNNING_MESSAGE
    return str(exc)


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[str, dict[str, str]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return text, {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        if _is_xhs_mcp_connection_refused(exc):
            raise ConnectionError(_XHS_MCP_NOT_RUNNING_MESSAGE) from exc
        raise RuntimeError(str(exc.reason)) from exc


def _mcp_response_objects(raw: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for line in raw.splitlines():
        item = line.strip()
        if item.startswith("data:"):
            item = item[5:].strip()
        if not item or not item.startswith("{"):
            continue
        try:
            value = json.loads(item)
        except ValueError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    if not objects:
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _xhs_mcp_call(tool_name: str, arguments: dict[str, Any] | None = None, *, timeout: float = 60.0) -> str:
    url = _xhs_mcp_url()
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "agentstrata-console", "version": "1.0"},
        },
    }
    _body, response_headers = _post_json(url, init, timeout=timeout)
    session_id = response_headers.get("mcp-session-id", "").strip()
    session_headers = {"Mcp-Session-Id": session_id} if session_id else {}

    _post_json(
        url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=session_headers,
        timeout=timeout,
    )
    body, _headers = _post_json(
        url,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        },
        headers=session_headers,
        timeout=timeout,
    )
    return body


def _first_mcp_error(raw: str) -> str | None:
    for obj in _mcp_response_objects(raw):
        error = obj.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or json.dumps(error, ensure_ascii=False))
        if obj.get("is_error") or obj.get("isError"):
            return json.dumps(obj, ensure_ascii=False)
    return None


def _extract_xhs_qrcode_data_url(raw: str) -> str:
    error = _first_mcp_error(raw)
    if error:
        raise ValueError(error)

    text_parts: list[str] = []
    for obj in _mcp_response_objects(raw):
        result = obj.get("result") if isinstance(obj, dict) else None
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "image":
                    data = str(item.get("data") or "").strip()
                    if not data:
                        continue
                    if data.startswith("data:image"):
                        return data
                    mime = str(item.get("mimeType") or item.get("mime_type") or "image/png")
                    return f"data:{mime};base64,{data}"
                if item.get("type") == "text":
                    text_parts.append(str(item.get("text") or ""))
        text_parts.append(json.dumps(obj, ensure_ascii=False))

    blob = "\n".join(text_parts) or raw
    data_url = re.search(r"data:image/(?:png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=\n\r]+", blob)
    if data_url:
        return data_url.group(0).replace("\n", "").replace("\r", "")
    img = re.search(r'"img"\s*:\s*"([^"]+)"', blob)
    if img:
        data = img.group(1).replace("\\n", "").replace("\n", "").replace("\r", "")
        if data.startswith("data:image"):
            return data
        return f"data:image/png;base64,{data}"
    raise ValueError("Xiaohongshu MCP response did not contain a QR code image.")


def _extract_mcp_text(raw: str) -> str:
    parts: list[str] = []
    for obj in _mcp_response_objects(raw):
        result = obj.get("result") if isinstance(obj, dict) else None
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
        if not parts:
            parts.append(json.dumps(obj, ensure_ascii=False))
    return "\n".join(parts) or raw


def _xhs_logged_in(raw: str) -> bool:
    text = _extract_mcp_text(raw)
    lowered = text.lower()
    if any(token in text for token in ("未登录", "需要登录", "登录已失效", "扫码")):
        return False
    if any(token in lowered for token in ("not logged", "login required", "need login")):
        return False
    if any(token in text for token in ("已登录", "登录成功", "已处于登录状态")):
        return True
    if any(token in lowered for token in ("logged in", "login ok", '"is_logged_in": true', '"logged_in": true')):
        return True
    return False


def _compact_text(text: str, *, limit: int = 1000) -> str:
    compact = " ".join(str(text).split())
    return compact if len(compact) <= limit else compact[:limit] + "...[truncated]"


def shared_service_xhs_start() -> Dict[str, object]:
    script = _docker_service_script()
    if not script.is_file():
        return {"ok": False, "error": f"missing script: {script}"}
    try:
        cp = run_capture(["bash", str(script), "start", "xiaohongshu-mcp"], timeout=120.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": cp.returncode == 0,
        "container": _XHS_MCP_CONTAINER,
        "stdout": (cp.stdout or "").strip(),
        "stderr": (cp.stderr or "").strip(),
    }


def _xhs_login_qrcode_response(raw: str) -> Dict[str, object]:
    if _xhs_logged_in(raw):
        return {
            "ok": True,
            "already_logged_in": True,
            "message": _compact_text(_extract_mcp_text(raw)),
        }
    return {
        "ok": True,
        "image_data_url": _extract_xhs_qrcode_data_url(raw),
    }


def shared_service_xhs_login_qrcode() -> Dict[str, object]:
    try:
        raw = _xhs_mcp_call("get_login_qrcode", {}, timeout=60.0)
        return _xhs_login_qrcode_response(raw)
    except ConnectionError as exc:
        start = shared_service_xhs_start()
        if not start.get("ok"):
            detail = start.get("error") or start.get("stderr") or _xhs_api_error_message(exc)
            return {
                "ok": False,
                "error": f"{_XHS_MCP_NOT_RUNNING_MESSAGE} 启动失败：{_compact_text(str(detail), limit=500)}",
                "start": start,
            }
        try:
            raw = _xhs_mcp_call("get_login_qrcode", {}, timeout=60.0)
            res = _xhs_login_qrcode_response(raw)
            res["started"] = True
            return res
        except Exception as retry_exc:  # noqa: BLE001 - API boundary returns structured failure.
            return {"ok": False, "error": _xhs_api_error_message(retry_exc), "start": start}
    except Exception as exc:  # noqa: BLE001 - API boundary returns structured failure.
        return {"ok": False, "error": _xhs_api_error_message(exc)}


def shared_service_xhs_check_login() -> Dict[str, object]:
    try:
        raw = _xhs_mcp_call("check_login_status", {}, timeout=30.0)
        return {
            "ok": True,
            "logged_in": _xhs_logged_in(raw),
            "message": _compact_text(_extract_mcp_text(raw)),
        }
    except Exception as exc:  # noqa: BLE001 - API boundary returns structured failure.
        return {"ok": False, "logged_in": False, "error": _xhs_api_error_message(exc)}


def control(inst: BotInstance, verb: str) -> Dict[str, object]:
    if verb not in {"start", "stop", "restart"}:
        return {"ok": False, "error": f"不支持的动作：{verb}"}
    if not systemd.is_available():
        return {"ok": False, "error": "systemd --user 不可用（WSL 是否开启 systemd？）"}
    if not instance_registered(inst):
        return {"ok": False, "error": "尚未注册 systemd，请先点卡片上的「注册服务」"}
    cp = run_capture(["bash", str(_console_script("ctl.sh")), verb, inst.instance_id])
    ok = cp.returncode == 0
    return {
        "ok": ok,
        "verb": verb,
        "instance_id": inst.instance_id,
        "stdout": (cp.stdout or "").strip(),
        "stderr": (cp.stderr or "").strip(),
        "status": status(inst),
    }


# ---------------------------------------------------------------------------
# 长操作收尾的重启段：未注册不算失败，给可执行提示即可（graceful 降级）。
# ---------------------------------------------------------------------------
def _restart_or_hint(inst: BotInstance) -> tuple[List[str], int]:
    if not systemd.is_available():
        return (["[WARN] systemd --user 不可用，未自动重启。"], 0)
    if not instance_registered(inst):
        return (["[WARN] 该实例未注册 systemd，未自动重启。请点卡片上的「注册服务」后再启动。"], 0)
    res = control(inst, "restart")
    if res.get("ok"):
        return ([f"[OK] {inst.unit_short} 已重启"], 0)
    return ([f"[ERR] 重启失败：{res.get('error') or res.get('stderr')}"], 1)


# ---------------------------------------------------------------------------
# 隔离护栏：机器人级操作（同步 / 重建）的目标目录绝不能等于控制仓库根，否则会
# 覆盖/重建控制台自身代码，导致「更新机器人」误伤控制台。
# ---------------------------------------------------------------------------
def _isolation_error(inst: BotInstance) -> Optional[str]:
    try:
        same = Path(inst.wsl_home).resolve() == repo_root().resolve()
    except OSError:
        return None
    if same:
        return (
            f"该实例 wsl_home 指向控制仓库根（{inst.wsl_home}），与控制台同目录。"
            "机器人操作已中止以保护控制台：请把 bot.yaml 的 deploy.wsl_home 设为独立目录"
            "（如 ~/ChatCopilot-<id>），并重跑 console/systemd/register.sh <id>。"
        )
    return None


# ---------------------------------------------------------------------------
# 同步代码（长操作，生成器）
# ---------------------------------------------------------------------------
def stream_sync(inst: BotInstance, *, dry_run: bool = False, restart_after: bool = True) -> Iterator[str]:
    iso = _isolation_error(inst)
    if iso:
        yield f"[ERR] {iso}"
        yield "__EXIT__ 1"
        return
    script = repo_root() / "deploy" / "wsl" / "sync_code.sh"
    if not script.is_file():
        yield f"[ERR] 找不到 {script}"
        yield "__EXIT__ 1"
        return
    args = ["bash", str(script), "--src", str(repo_root()), "--dst", inst.wsl_home]
    if dry_run:
        args.append("--dry-run")
    yield f"[console] 同步 {inst.instance_id}: {repo_root()} -> {inst.wsl_home}"
    sync_rc = 0
    for line in run_streaming(args, cwd=str(repo_root())):
        if line.startswith("__EXIT__"):
            sync_rc = int(line.split()[1])
            break
        yield line
    if sync_rc != 0:
        yield f"[ERR] 同步失败（exit {sync_rc}）"
        yield f"__EXIT__ {sync_rc}"
        return
    if dry_run or not restart_after:
        yield "__EXIT__ 0"
        return
    yield "[console] 同步完成，重启服务..."
    lines, code = _restart_or_hint(inst)
    for ln in lines:
        yield ln
    yield f"__EXIT__ {code}"


# ---------------------------------------------------------------------------
# 重建环境（长操作）
# ---------------------------------------------------------------------------
def stream_rebuild(inst: BotInstance, *, restart_after: bool = True) -> Iterator[str]:
    iso = _isolation_error(inst)
    if iso:
        yield f"[ERR] {iso}"
        yield "__EXIT__ 1"
        return
    bootstrap = Path(inst.wsl_home) / "deploy" / "wsl" / "bootstrap_wsl.sh"
    if not bootstrap.is_file():
        yield f"[ERR] 找不到 {bootstrap}，请先点「更新代码并重启」把代码同步到该实例"
        yield "__EXIT__ 1"
        return
    yield f"[console] 重建环境 {inst.instance_id}（bootstrap_wsl.sh，耗时较长）..."
    rc = 0
    for line in run_streaming(
        ["bash", str(bootstrap)],
        cwd=inst.wsl_home,
        extra_env={"CHATCOPILOT_BOT_SPEC": inst.bot_spec, "CHATCOPILOT_INSTANCE_ID": inst.instance_id},
    ):
        if line.startswith("__EXIT__"):
            rc = int(line.split()[1])
            break
        yield line
    if rc != 0:
        yield f"[ERR] 重建失败（exit {rc}）"
        yield f"__EXIT__ {rc}"
        return
    if restart_after:
        yield "[console] 重建完成，重启服务..."
        lines, code = _restart_or_hint(inst)
        for ln in lines:
            yield ln
        yield f"__EXIT__ {code}"
        return
    yield "__EXIT__ 0"


# ---------------------------------------------------------------------------
# 诊断快照（长操作）
# ---------------------------------------------------------------------------
def stream_update(inst: BotInstance, *, dry_run: bool = False) -> Iterator[str]:
    """Run the adaptive instance update path: provision, sync, apply/rebuild, restart."""
    iso = _isolation_error(inst)
    if iso:
        yield f"[ERR] {iso}"
        yield "__EXIT__ 1"
        return
    script = repo_root() / "deploy" / "wsl" / "update_instance.sh"
    if not script.is_file():
        yield f"[ERR] 找不到 {script}"
        yield "__EXIT__ 1"
        return
    args = [
        "bash",
        str(script),
        "--instance",
        inst.instance_id,
        "--src",
        str(repo_root()),
        "--dst",
        inst.wsl_home,
        "--bot",
        inst.bot_spec,
    ]
    if dry_run:
        args.append("--dry-run")
    yield f"[console] 一键更新 {inst.instance_id}: provision-env -> sync -> apply/rebuild -> restart"
    yield from run_streaming(args, cwd=str(repo_root()))


def stream_dump(inst: BotInstance, *, mode: str = "quick") -> Iterator[str]:
    script = repo_root() / "deploy" / "wsl" / "dump.sh"
    if not script.is_file():
        yield f"[ERR] 找不到 {script}"
        yield "__EXIT__ 1"
        return
    yield f"[console] 诊断快照 {inst.instance_id}（mode={mode}）..."
    yield from run_streaming(
        ["bash", str(script), "--instance", inst.instance_id, "--mode", mode],
        cwd=str(repo_root()),
    )


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    return load_yaml_mapping_or_empty(path)


def _bot_spec_path(inst: BotInstance) -> Path:
    raw = Path(inst.bot_spec)
    if raw.is_absolute():
        return raw
    return repo_root() / raw


# ---------------------------------------------------------------------------
# 首次部署：共享 ProvisionPlan + bot-owned local.env 安全 patch
# ---------------------------------------------------------------------------
def _provision_context(inst: BotInstance):
    candidate = _bot_spec_path(inst)
    bots_root = (repo_root() / "bots").resolve(strict=True)
    if candidate.is_symlink() or candidate.parent.is_symlink():
        raise ProvisioningError("provision_botspec_symlink")
    bot_yaml = candidate.resolve(strict=True)
    if (
        bot_yaml.name != "bot.yaml"
        or bot_yaml.parent.parent != bots_root
        or bot_yaml.parent.name != inst.instance_id
    ):
        raise ProvisioningError("provision_botspec_outside_repo")
    spec = load_botspec(bot_yaml)
    adapter = get_adapter(spec.platform.type)
    local_env = bot_yaml.parent / "local.env"
    configured_values = read_local_env_for_provision(
        local_env,
        allowed_parent=bot_yaml.parent,
    )
    plan = build_provision_plan(spec, adapter, configured_values=configured_values)
    return bot_yaml, local_env, adapter, plan


def provision_schema(inst: BotInstance) -> Dict[str, object]:
    _bot_yaml, _local_env, adapter, plan = _provision_context(inst)
    plan_payload = plan.to_dict()
    rendered_fields = list(plan_payload.get("fields", []))
    return {
        "schema_version": 2,
        "platform": plan.platform,
        "adapter_id": adapter.adapter_id,
        "bot_id": plan.bot_id,
        "requires_code_worker": plan.requires_code_worker,
        "common_fields": [field for field in rendered_fields if field.get("group") != "platform"],
        "fields": [field for field in rendered_fields if field.get("group") == "platform"],
        "setup_actions": [
            {
                "id": action.id,
                "label": action.label,
                "description": action.description,
                "guided_surface": getattr(action, "guided_surface", "console"),
                "default_verb": getattr(action, "default_verb", "start"),
            }
            for action in adapter.setup_actions()
        ],
        "shared_services": _shared_service_steps(inst),
    }


def _bot_tool_pack_ids(bot_data: dict[str, Any]) -> set[str]:
    tools = bot_data.get("tools") if isinstance(bot_data.get("tools"), dict) else {}
    included = tools.get("packs") if isinstance(tools, dict) else []
    if not isinstance(included, list):
        return set()
    return {str(item).strip() for item in included if str(item).strip()}


_TOOL_PACK_NAMESPACE_LABELS: dict[str, str] = {
    "chat": "会话能力",
    "feishu": "飞书工具",
    "filesystem": "文件系统",
    "unity": "Unity 代码库",
    "workspace": "Workspace 工具",
    "memory": "记忆工具",
    "persona": "Persona 工具",
    "playbooks": "任务剧本工具",
    "mcp": "MCP 管理",
    "web": "网页读取",
    "codebase": "代码仓库",
    "career": "职业情报",
}


def _bot_tool_packs_grouped(bot_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return tool packs from BotSpec grouped by namespace."""
    packs = _bot_tool_pack_ids(bot_data)
    if not packs:
        return []
    groups: dict[str, list[str]] = {}
    for pack in sorted(packs):
        ns = pack.split(".")[0] if "." in pack else pack
        groups.setdefault(ns, []).append(pack)
    return [
        {
            "namespace": ns,
            "label": _TOOL_PACK_NAMESPACE_LABELS.get(ns, ns),
            "tool_packs": items,
        }
        for ns, items in groups.items()
    ]


def _bot_uses_mcp_ref(inst: BotInstance, ref: str) -> bool:
    bot_yaml = _bot_spec_path(inst)
    data = _load_yaml_mapping(bot_yaml)
    tools = data.get("tools") if isinstance(data.get("tools"), dict) else {}
    mcp = tools.get("mcp") if isinstance(tools.get("mcp"), dict) else {}
    servers_value = mcp.get("servers") if isinstance(mcp, dict) else None
    if not servers_value:
        return False
    servers_path = Path(str(servers_value))
    if not servers_path.is_absolute():
        servers_path = bot_yaml.parent / servers_path
    servers_data = _load_yaml_mapping(servers_path)
    servers = servers_data.get("servers", [])
    if not isinstance(servers, list):
        return False
    for item in servers:
        if not isinstance(item, dict):
            continue
        if not _config_enabled(item.get("enabled"), default=True):
            continue
        if str(item.get("ref") or "").strip() == ref:
            return True
    return False


def _shared_service_steps(inst: BotInstance) -> list[dict[str, object]]:
    if not _bot_uses_mcp_ref(inst, _XHS_MCP_REF):
        return []
    return [
        {
            "id": "xhs-login",
            "service": "xhs",
            "label": "小红书登录",
            "description": "启动小红书 MCP 并扫码登录，登录成功后继续部署。",
            "required": True,
        }
    ]


def write_instance_env(inst: BotInstance, secrets: Dict[str, str]) -> Dict[str, object]:
    """安全 patch bot-owned local.env，并生成实例运行时 env。

    local.env 是本机私有配置源；运行时 env 由 ``chatcopilot bot provision-env``
    从 bot.yaml + local.env 生成，避免控制台重复实现路径推导。
    """
    try:
        bot_yaml, local_env, adapter, plan = _provision_context(inst)
        by_alias = {
            alias: field
            for field in plan.fields
            for alias in (field.field, field.env_key)
        }
        for key, value in secrets.items():
            field = by_alias.get(str(key))
            if field is not None and field.host_generated and str(value or "").strip():
                return {
                    "ok": False,
                    "stage": "local_env",
                    "error": f"host_generated_field_must_not_be_submitted:{field.field}",
                }
        receipt = patch_local_env(
            local_env,
            plan,
            secrets,
            adapter=adapter,
            allowed_parent=bot_yaml.parent,
        )
    except (OSError, ValueError, ProvisioningError) as exc:
        message = str(exc)
        if message.startswith("unknown_provision_field:"):
            message = "unknown_provision_field"
        return {"ok": False, "stage": "local_env", "error": message}

    receipt_payload = receipt.to_dict()
    env_key_by_field = {field.field: field.env_key for field in plan.fields}
    written_keys = [
        env_key_by_field.get(field, field)
        for field in receipt_payload.get("changed_fields", [])
    ]

    env = dict(os.environ)
    env["PYTHONPATH"] = f"{repo_root() / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    try:
        provision = subprocess.run(
            [
                sys.executable,
                "-m",
                "chatcopilot",
                "bot",
                "provision-env",
                "--bot",
                str(bot_yaml),
            ],
            cwd=str(repo_root()),
            env=env,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "stage": "runtime_env",
            "error": f"调用 bot provision-env 失败：{exc}",
            "receipt": receipt_payload,
        }
    if provision.returncode != 0:
        return {
            "ok": False,
            "stage": "runtime_env",
            "error": (provision.stderr or provision.stdout or "生成运行时 env 失败").strip(),
            "receipt": receipt_payload,
        }

    return {
        "ok": True,
        "env_file": inst.env_file,
        "local_env_file": str(local_env),
        "written_keys": written_keys,
        "receipt": receipt_payload,
    }


def stream_setup_action(inst: BotInstance, action_id: str, verb: str = "start") -> Iterator[str]:
    action = next(
        (item for item in get_adapter(inst.platform).setup_actions() if item.id == action_id),
        None,
    )
    if action is None:
        yield f"[ERR] 实例 {inst.instance_id} 不支持 setup action: {action_id}"
        yield "__EXIT__ 2"
        return
    if action.allowed_verbs and verb not in action.allowed_verbs:
        yield f"[ERR] setup action {action_id} 不支持动作：{verb}"
        yield "__EXIT__ 2"
        return
    if not action.command:
        yield f"[OK] setup action {action_id} 无需额外执行"
        yield "__EXIT__ 0"
        return

    try:
        args = [
            part.format(
                instance_id=inst.instance_id,
                verb=verb,
                repo_root=str(repo_root()),
                bot_spec=inst.bot_spec,
                platform=inst.platform,
            )
            for part in action.command
        ]
    except KeyError as exc:
        yield f"[ERR] setup action {action_id} 命令模板含未知变量：{exc}"
        yield "__EXIT__ 2"
        return
    yield f"[console] setup action {action_id} {verb}: {inst.instance_id}"
    yield from run_streaming(args, cwd=str(repo_root()))


# ---------------------------------------------------------------------------
# 首次部署：注册 systemd 模板服务（长操作，生成器）
# ---------------------------------------------------------------------------
def stream_register(inst: BotInstance) -> Iterator[str]:
    script = repo_root() / "console" / "systemd" / "register.sh"
    if not script.is_file():
        yield f"[ERR] 找不到 {script}"
        yield "__EXIT__ 1"
        return
    yield f"[console] 注册 systemd 服务 chatcopilot@{inst.instance_id} ..."
    yield from run_streaming(["bash", str(script), inst.instance_id], cwd=str(repo_root()))
