"""Development account readiness; does not rewrite shared model settings."""
import os
from pathlib import Path
import subprocess
import sys

from fastapi import APIRouter, Depends, HTTPException, Request

from console.backend.routes.applications import access, service, call
from chatcopilot.core.model_settings import resolve_binding, worker_profile
from chatcopilot.harness.config import configuration

router = APIRouter(prefix="/api/development/setup", dependencies=[Depends(access)])


def environment(manager):
    return {**os.environ, **configuration(), "AGENTSTRATA_LLM_CONFIG": str(manager.model_path)}


def account(manager, lane):
    env = environment(manager)
    cfg = (worker_profile(environment=env)[0] if lane == "worker"
           else resolve_binding("chat", environment=env))
    if cfg.auth_mode != "chatgpt" or cfg.auth_profile != lane:
        raise ValueError("此模型用途没有绑定对应的 Codex 授权通道")
    if not cfg.credential_root or not cfg.codex_bin:
        raise ValueError("请先配置开发账号目录与 Codex 执行文件")
    return cfg


@router.get("")
def status(request: Request):
    manager = service(request)
    from chatcopilot.core.model_settings import read_settings
    from chatcopilot.external_tools.codex_cli.auth_cli import status_lanes
    data = call(lambda: read_settings(environment(manager)))
    auth = []
    for lane in ("main", "worker"):
        try:
            cfg = account(manager, lane)
            row = status_lanes(Path(cfg.credential_root), lane)[0].to_dict()
            auth.append({**row, "login_available": bool(os.environ.get("WSL_DISTRO_NAME"))})
        except (ValueError, OSError):
            auth.append({"lane": lane, "state": "unconfigured", "login_available": False})
    bindings = {key: bool(data["bindings"].get(key)) for key in ("chat", "harness", "evaluation.judge")}
    return {"configured": bindings["chat"], "bindings": bindings, "auth": auth,
            "login_available": any(row["login_available"] for row in auth)}


@router.post("/login/{lane}")
def login(request: Request, lane: str):
    if lane not in {"main", "worker"}:
        raise HTTPException(400, "未知授权类型")
    manager = service(request)
    cfg = call(lambda: account(manager, lane))
    distro = os.environ.get("WSL_DISTRO_NAME")
    if not distro:
        raise HTTPException(409, "请在 Windows 开发启动器中完成授权")
    import pwd
    from studio.delivery import windows_path
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        windows_path(manager.root / "studio/windows/login.ps1"),
                        "-Distro", distro, "-User", pwd.getpwuid(os.getuid()).pw_name,
                        "-Framework", str(manager.root), "-Python", sys.executable,
                        "-StudioHome", str(manager.state_dir), "-Lane", lane,
                        "-AuthRoot", cfg.credential_root, "-CodexBin", cfg.codex_bin],
                       check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise HTTPException(503, "授权窗口未能打开，请检查本机开发环境") from None
    return {"message": "已打开本机授权窗口，请按提示完成登录"}
