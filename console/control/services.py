"""基础设施服务管理：声明式 catalog + 按 service_type 分组的纯函数。

支持两类外部服务：
- compose: docker-compose 管理（SearXNG engine / 小红书 / Playwright MCP）
- standalone: 独立 docker 容器（外部 NapCat OneBot provider）

Bot 级内嵌工具包（Feishu Tools / workspace 等）不在此 catalog，
由 status API 的 tool_packs 字段从 BotSpec 读取并按 namespace 分组返回。

新增服务只需在 SERVICES 中加一条 ServiceDef。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal

from console.control.discovery import repo_root
from chatcopilot.botspec.loader import is_valid_bot_id
from chatcopilot.botspec.provisioning import read_local_env_for_provision
from chatcopilot.platforms.qq.boundary import (
    require_access_token,
    require_loopback_websocket_url,
)
from chatcopilot.platforms.qq.gateway_health import (
    OneBotRuntimeStatus,
    query_onebot_runtime_status,
)
from chatcopilot.platforms.qq.webui_session import (
    NapCatWebUiError,
    check_login_status as check_napcat_login_status,
    read_webui_session,
)

ServiceType = Literal["compose", "standalone", "embedded", "remote"]

# ---------------------------------------------------------------------------
# 登录状态缓存（TTL-based，避免每次状态轮询触发 MCP 调用）
# ---------------------------------------------------------------------------
_LOGIN_STATE_CACHE: dict[str, tuple[str | None, float]] = {}
_DOCKER_INSPECT_TIMEOUT = float(os.environ.get("CHATCOPILOT_CONSOLE_DOCKER_INSPECT_TIMEOUT", "1.0"))
_LOGIN_CACHE_TTL = 120.0  # 秒
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# ---------------------------------------------------------------------------
# all_services_status 结果缓存（TTL-based，避免多路并发请求重复触发 docker inspect）
# ---------------------------------------------------------------------------
_STATUS_CACHE: tuple[list[dict[str, Any]], float] | None = None
_STATUS_CACHE_TTL = float(os.environ.get("CHATCOPILOT_CONSOLE_STATUS_CACHE_TTL", "10.0"))


def get_cached_login_state(service_id: str) -> str | None:
    """Return cached login state or None if unknown / expired."""
    entry = _LOGIN_STATE_CACHE.get(service_id)
    if entry and (time.time() - entry[1]) < _LOGIN_CACHE_TTL:
        return entry[0]
    return None


def update_login_cache(service_id: str, state: str | None) -> None:
    """Update login state cache after an explicit check."""
    _LOGIN_STATE_CACHE[service_id] = (state, time.time())


@dataclass(frozen=True)
class ServiceDef:
    id: str
    display_name: str
    service_type: ServiceType
    # compose
    container: str = ""
    compose_service: str = ""
    compose_file: str = ""
    # standalone
    container_prefix: str = ""
    bound_instance_ids: tuple[str, ...] = ()
    # embedded
    env_key: str = ""
    # tool packs
    actions: tuple[str, ...] = ()
    has_login: bool = False
    has_doctor: bool = False
    mcp_refs: tuple[str, ...] = ()
    search_provider_kinds: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    tool_pack_ids: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)


SERVICES: tuple[ServiceDef, ...] = (
    ServiceDef(
        id="xiaohongshu",
        display_name="小红书 MCP",
        service_type="compose",
        container="chatcopilot-xiaohongshu-mcp",
        compose_service="xiaohongshu-mcp",
        compose_file="deploy/docker/docker-compose.yaml",
        actions=("start", "stop", "restart", "pull"),
        has_login=True,
        has_doctor=True,
        mcp_refs=("xiaohongshu-search",),
        extra={"login_type": "qrcode"},
    ),
    ServiceDef(
        id="searxng",
        display_name="SearXNG Search Engine",
        service_type="compose",
        container="chatcopilot-searxng",
        compose_service="searxng",
        compose_file="deploy/docker/docker-compose.yaml",
        actions=("start", "stop", "restart", "pull"),
        has_doctor=True,
        search_provider_kinds=("searxng",),
    ),
    ServiceDef(
        id="playwright",
        display_name="Playwright Browser MCP",
        service_type="compose",
        container="chatcopilot-playwright-mcp",
        compose_service="playwright-mcp",
        compose_file="deploy/docker/docker-compose.yaml",
        actions=("start", "stop", "restart", "pull"),
        has_doctor=True,
        mcp_refs=("playwright-browser",),
    ),
    ServiceDef(
        id="napcat",
        display_name="NapCat OneBot Provider",
        service_type="standalone",
        container_prefix="napcat-",
        bound_instance_ids=("lingye-copilot-qq",),
        actions=("start", "stop", "restart"),
        has_login=True,
        has_doctor=True,
        platforms=("qq",),
        extra={"login_type": "webui_link"},
    ),
    ServiceDef(
        id="github",
        display_name="GitHub MCP (readonly)",
        service_type="remote",
        mcp_refs=("github-readonly",),
    ),
)


def find_service(service_id: str) -> ServiceDef | None:
    return next((s for s in SERVICES if s.id == service_id), None)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def _compute_services_status() -> list[dict[str, Any]]:
    """Run all docker/status checks in parallel and return ordered results."""
    tasks: list[tuple[int, Any]] = []
    for svc in SERVICES:
        if svc.service_type == "compose":
            tasks.append((len(tasks), (svc, None, "compose")))
        elif svc.service_type == "standalone":
            for inst_id in svc.bound_instance_ids:
                tasks.append((len(tasks), (svc, inst_id, "standalone")))
        elif svc.service_type == "embedded":
            tasks.append((len(tasks), (svc, None, "embedded")))
        elif svc.service_type == "remote":
            tasks.append((len(tasks), (svc, None, "remote")))

    results: dict[int, dict[str, Any]] = {}

    def _run(idx: int, svc: Any, inst_id: Any, kind: str) -> tuple[int, dict[str, Any]]:
        if kind == "compose":
            return idx, compose_status(svc)
        if kind == "standalone":
            return idx, standalone_status(svc, inst_id)
        if kind == "embedded":
            return idx, embedded_status(svc)
        return idx, remote_status(svc)

    with ThreadPoolExecutor(max_workers=min(len(tasks) or 1, 8)) as pool:
        futures = {
            pool.submit(_run, idx, svc, inst_id, kind): idx
            for idx, (svc, inst_id, kind) in tasks
        }
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = result

    return [results[i] for i in sorted(results)]


def all_services_status() -> list[dict[str, Any]]:
    """Return cached or freshly computed service status list."""
    global _STATUS_CACHE
    now = time.monotonic()
    if _STATUS_CACHE is not None:
        cached, ts = _STATUS_CACHE
        if now - ts < _STATUS_CACHE_TTL:
            return cached
    result = _compute_services_status()
    _STATUS_CACHE = (result, now)
    return result


def invalidate_status_cache() -> None:
    """Force-expire the status cache (e.g., after an action that changes container state)."""
    global _STATUS_CACHE
    _STATUS_CACHE = None


# ---------------------------------------------------------------------------
# compose 类型（docker-compose 管理的服务）
# ---------------------------------------------------------------------------

def _docker_inspect(container: str) -> dict[str, Any] | None:
    try:
        cp = subprocess.run(
            [
                "docker", "inspect", "--format",
                '{"status":"{{.State.Status}}",'
                '"health":"{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",'
                '"running":{{.State.Running}},'
                '"started_at":"{{.State.StartedAt}}"}',
                container,
            ],
            capture_output=True, text=True, timeout=_DOCKER_INSPECT_TIMEOUT,
        )
        if cp.returncode != 0:
            return None
        return json.loads(cp.stdout.strip())
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _container_uptime_s(started_at: str) -> int | None:
    if not started_at or started_at.startswith("0001"):
        return None
    try:
        from datetime import datetime, timezone
        started_at = started_at.replace("Z", "+00:00")
        if "." in started_at:
            dot_idx = started_at.index(".")
            plus_idx = started_at.index("+", dot_idx) if "+" in started_at[dot_idx:] else started_at.index("-", dot_idx + 1)
            frac = started_at[dot_idx + 1:plus_idx][:6]
            started_at = started_at[:dot_idx + 1] + frac + started_at[plus_idx:]
        dt = datetime.fromisoformat(started_at)
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except (ValueError, TypeError):
        return None


def _health_to_color(health: str, running: bool) -> tuple[str, str]:
    """Return (state, color) from docker health/running info."""
    if health == "healthy":
        return "healthy", "green"
    if health in ("starting", "none") and running:
        return "running", "yellow"
    if not running:
        return "stopped", "red"
    return "unhealthy", "red"


def compose_status(svc: ServiceDef) -> dict[str, Any]:
    info = _docker_inspect(svc.container)
    if info is None:
        return _base_status(svc, state="not_found", color="grey")
    running = bool(info.get("running", False))
    health = str(info.get("health", "none"))
    state, color = _health_to_color(health, running)
    uptime = _container_uptime_s(str(info.get("started_at", ""))) if running else None
    result = _base_status(svc, state=state, color=color)
    result.update(container=svc.container, uptime_s=uptime)
    return result


def compose_up_all() -> dict[str, Any]:
    """Reconcile shared Compose services to enabled BotSpec desired state."""
    script = repo_root() / "deploy" / "docker" / "services.sh"
    if not script.is_file():
        return {"ok": False, "error": f"shared service manager not found: {script}"}
    try:
        cp = subprocess.run(
            ["bash", str(script), "start"],
            capture_output=True,
            text=True,
            timeout=300.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    invalidate_status_cache()
    return {
        "ok": cp.returncode == 0,
        "stdout": (cp.stdout or "").strip(),
        "stderr": (cp.stderr or "").strip(),
    }


def compose_action(svc: ServiceDef, verb: str) -> dict[str, Any]:
    compose_file = str(repo_root() / svc.compose_file)
    if verb == "pull":
        return _compose_run(compose_file, ["pull", svc.compose_service])
    if verb == "start":
        return _compose_run(compose_file, ["up", "-d", svc.compose_service])
    if verb == "stop":
        return _compose_run(compose_file, ["stop", svc.compose_service])
    if verb == "restart":
        return _compose_run(compose_file, ["restart", svc.compose_service])
    return {"ok": False, "error": f"不支持的动作：{verb}"}


def _compose_run(compose_file: str, args: list[str]) -> dict[str, Any]:
    cmd = ["docker", "compose", "-f", compose_file, *args]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=120.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": cp.returncode == 0,
        "stdout": (cp.stdout or "").strip(),
        "stderr": (cp.stderr or "").strip(),
    }


_DOCTOR_TARGETS: dict[str, str] = {
    "xiaohongshu": "xhs",
    "searxng": "searxng",
    "playwright": "playwright",
}


def compose_action_streaming(svc: ServiceDef, verb: str) -> Iterator[str]:
    """Long-running compose actions (pull / doctor) as streaming output."""
    compose_file = str(repo_root() / svc.compose_file)
    if verb == "pull":
        args = ["docker", "compose", "-f", compose_file, "pull", svc.compose_service]
    elif verb == "doctor":
        script = repo_root() / "deploy" / "docker" / "services.sh"
        doctor_target = _DOCTOR_TARGETS.get(svc.id, svc.id)
        args = ["bash", str(script), "doctor", doctor_target]
    else:
        yield f"[ERR] 不支持流式动作：{verb}"
        yield "__EXIT__ 2"
        return

    yield from _command_streaming(args, intro=f"[infra] {svc.display_name}: {verb}")


def doctor_streaming(svc: ServiceDef, instance_id: str | None = None) -> Iterator[str]:
    """Run a service doctor without entering the Agent Evaluation lifecycle."""

    if svc.service_type == "compose":
        yield from compose_action_streaming(svc, "doctor")
        return
    if svc.service_type == "standalone" and svc.id == "napcat" and instance_id:
        script = repo_root() / "deploy" / "wsl" / "qq_gateway.sh"
        args = ["bash", str(script), "status", "--instance", instance_id]
        yield from _command_streaming(
            args,
            intro=f"[external-check] {svc.display_name}: {instance_id}",
        )
        return
    yield f"[ERR] 服务不支持外部诊断：{svc.id}"
    yield "__EXIT__ 2"


def _command_streaming(args: list[str], *, intro: str) -> Iterator[str]:
    yield intro
    env = dict(os.environ)
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
    except OSError as exc:
        yield f"[ERR] 无法启动：{exc}"
        yield "__EXIT__ 127"
        return
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line.rstrip("\n")
    proc.wait()
    yield f"__EXIT__ {proc.returncode}"


def compose_logs(svc: ServiceDef) -> Iterator[str]:
    cmd = ["docker", "logs", "-f", "--tail", "200", svc.container]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except OSError as exc:
        yield f"[ERR] {exc}"
        return
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line.rstrip("\n")


# ---------------------------------------------------------------------------
# standalone 类型（独立 docker 容器）
# ---------------------------------------------------------------------------

def _standalone_container(svc: ServiceDef, instance_id: str) -> str:
    return f"{svc.container_prefix}{instance_id}"


def standalone_status(svc: ServiceDef, instance_id: str) -> dict[str, Any]:
    container = _standalone_container(svc, instance_id)
    info = _docker_inspect(container)
    if info is None:
        result = _base_status(svc, state="not_found", color="grey")
        result["instance_id"] = instance_id
        result["id"] = f"{svc.id}:{instance_id}"
        return result
    running = bool(info.get("running", False))
    health = str(info.get("health", "none"))
    state, color = _health_to_color(health, running)
    runtime_status: OneBotRuntimeStatus | None = None
    runtime_status_unknown = False
    if svc.id == "napcat" and running:
        try:
            runtime_status = _napcat_onebot_runtime_status(instance_id)
        except Exception:  # noqa: BLE001 - project a bounded unknown state to Console
            runtime_status_unknown = True
            update_login_cache(svc.id, None)
            state, color = "running", "yellow"
        else:
            update_login_cache(
                svc.id,
                "logged_in" if runtime_status.online else "logged_out",
            )
            if runtime_status.online and runtime_status.good:
                state, color = "healthy", "green"
            else:
                state, color = "unhealthy", "red"
    uptime = _container_uptime_s(str(info.get("started_at", ""))) if running else None
    result = _base_status(svc, state=state, color=color)
    result.update(
        id=f"{svc.id}:{instance_id}",
        container=container,
        uptime_s=uptime,
        instance_id=instance_id,
        account_online=runtime_status.online if runtime_status is not None else None,
        provider_good=runtime_status.good if runtime_status is not None else None,
    )
    if runtime_status_unknown:
        message = "QQ account login state could not be verified."
        result["checks"].append(
            {"name": "login", "ok": False, "severity": "warning", "message": message}
        )
        result["reasons"].append(message)
    return result


def standalone_action(svc: ServiceDef, instance_id: str, verb: str) -> dict[str, Any]:
    container = _standalone_container(svc, instance_id)
    if svc.id == "napcat" and verb in {"start", "restart"}:
        return _napcat_provider_action(instance_id, verb)
    if verb == "start":
        return _docker_simple(["docker", "start", container])
    if verb == "stop":
        return _docker_simple(["docker", "stop", container])
    if verb == "restart":
        return _docker_simple(["docker", "restart", container])
    return {"ok": False, "error": f"不支持的动作：{verb}"}


def _napcat_provider_action(instance_id: str, action: str) -> dict[str, Any]:
    """Run a guarded external NapCat provider lifecycle action."""
    if action not in {"bootstrap", "start", "restart", "sync-token"}:
        return {"ok": False, "error": f"unsupported NapCat provider action: {action}"}
    script = repo_root() / "deploy" / "wsl" / "qq_gateway.sh"
    try:
        cp = subprocess.run(
            ["bash", str(script), action, "--instance", instance_id],
            capture_output=True,
            text=True,
            timeout=120.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": cp.returncode == 0,
        "stdout": _ANSI_ESCAPE_RE.sub("", cp.stdout or "").strip(),
        "stderr": _ANSI_ESCAPE_RE.sub("", cp.stderr or "").strip(),
    }


def _docker_simple(cmd: list[str]) -> dict[str, Any]:
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": cp.returncode == 0,
        "stdout": (cp.stdout or "").strip(),
        "stderr": (cp.stderr or "").strip(),
    }


def standalone_logs(svc: ServiceDef, instance_id: str) -> Iterator[str]:
    container = _standalone_container(svc, instance_id)
    cmd = ["docker", "logs", "-f", "--tail", "200", container]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except OSError as exc:
        yield f"[ERR] {exc}"
        return
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line.rstrip("\n")


# ---------------------------------------------------------------------------
# NapCat 登录状态（优先使用 OneBot，WebUI 仅作 bootstrap fallback）
# ---------------------------------------------------------------------------


def _napcat_onebot_runtime_status(instance_id: str) -> OneBotRuntimeStatus:
    if not is_valid_bot_id(instance_id):
        raise ValueError("invalid bot instance id")
    bot_dir = repo_root() / "bots" / instance_id
    values = read_local_env_for_provision(
        bot_dir / "local.env",
        allowed_parent=bot_dir,
    )
    url = require_loopback_websocket_url(
        values.get("CHATCOPILOT_QQ_ONEBOT_WS_URL") or "ws://127.0.0.1:3001",
        env_key="CHATCOPILOT_QQ_ONEBOT_WS_URL",
    )
    token = require_access_token(values.get("QQ_ACCESS_TOKEN"))
    return asyncio.run(query_onebot_runtime_status(url, token))


def standalone_webui_login_status(
    svc: ServiceDef,
    instance_id: str,
    *,
    host: str = "localhost",
    port: str = "6099",
) -> dict[str, Any]:
    """Return a bounded QQ login projection without exposing local credentials."""

    try:
        runtime_status = _napcat_onebot_runtime_status(instance_id)
    except Exception:  # noqa: BLE001 - WebUI remains available during initial bootstrap
        runtime_status = None
    if runtime_status is not None:
        return {
            "ok": True,
            "logged_in": runtime_status.online,
            "is_login": runtime_status.online,
            "is_offline": not runtime_status.online,
            "provider_good": runtime_status.good,
            "login_error": "" if runtime_status.good else "OneBot provider state is unhealthy",
        }

    container = _standalone_container(svc, instance_id)
    try:
        session = read_webui_session(container, host=host, port=port)
        status = check_napcat_login_status(session)
    except NapCatWebUiError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "logged_in": status.is_login,
        "is_login": status.is_login,
        "is_offline": status.is_offline,
        "provider_good": None,
        "login_error": status.login_error,
    }


def embedded_status(svc: ServiceDef) -> dict[str, Any]:
    configured = not svc.env_key or bool(os.environ.get(svc.env_key, "").strip())
    state = "configured" if configured else "unconfigured"
    color = "green" if configured else "grey"
    return _base_status(svc, state=state, color=color)


# ---------------------------------------------------------------------------
# remote 类型（远端 HTTP MCP，无本地容器）
# ---------------------------------------------------------------------------

def remote_status(svc: ServiceDef) -> dict[str, Any]:
    return _base_status(svc, state="enabled", color="green")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _check_env_configured(svc: ServiceDef) -> bool:
    """Check if the required env key is configured in the appropriate source.

    For compose services, read from deploy/docker/.env (where Docker picks it up).
    For others, fall back to the console process's own environment.
    """
    key = svc.env_key
    if not key:
        return False
    if svc.service_type == "compose" and svc.compose_file:
        dotenv_path = repo_root() / "deploy" / "docker" / ".env"
        if dotenv_path.is_file():
            try:
                for line in dotenv_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == key and v.strip():
                            return True
            except OSError:
                pass
        return False
    return bool(os.environ.get(key, "").strip())


def _base_status(svc: ServiceDef, *, state: str, color: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": svc.id,
        "display_name": svc.display_name,
        "service_type": svc.service_type,
        "state": state,
        "color": color,
        "container": None,
        "uptime_s": None,
        "actions": list(svc.actions),
        "has_login": svc.has_login,
        "has_doctor": svc.has_doctor,
        "instance_id": None,
        "login_state": get_cached_login_state(svc.id) if svc.has_login else None,
        "login_type": svc.extra.get("login_type") if svc.has_login else None,
        "account_online": None,
        "provider_good": None,
        "mcp_refs": list(svc.mcp_refs),
        "search_provider_kinds": list(svc.search_provider_kinds),
        "platforms": list(svc.platforms),
        "tool_pack_ids": list(svc.tool_pack_ids),
        "extra": dict(svc.extra),
    }
    if svc.env_key:
        result["env_configured"] = _check_env_configured(svc)
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []

    def add(name: str, ok: bool, severity: str, message: str) -> None:
        checks.append({"name": name, "ok": ok, "severity": severity, "message": message})
        if not ok:
            reasons.append(message)

    add("state", color in {"green", "yellow"}, "critical", f"service state is {state}.")
    if result.get("env_configured") is False:
        add("env", False, "warning", "required environment variable is not configured.")
    if svc.has_login and result.get("login_state") == "logged_out":
        add("login", False, "warning", "login state is logged out.")
    result["checks"] = checks
    result["reasons"] = reasons
    return result
