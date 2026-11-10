"""Own the developer's local model settings and unprivileged guide Gateway process."""
from pathlib import Path
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading

from fastapi import APIRouter
from pydantic import BaseModel
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.model_settings import ModelSettingsStore
from chatcopilot.core.private_sqlite import private_directory


class Models(BaseModel):
    model: str
    worker: str = ""
    endpoint: str = ""
    key: str = ""
    chatgpt: bool = False


def studio_environment(home):
    from chatcopilot.harness.config import configuration
    settings = configuration()
    return {**{key: settings[key] for key in ("CHATCOPILOT_HARNESS_UV_BIN",) if key in settings}, "AGENTSTRATA_LLM_CONFIG": str(home / "models.json"),
            "CHATCOPILOT_CODEX_BOT_HOME": str(home / "auth"),
            "CHATCOPILOT_CODEX_BIN": os.environ.get("AGENTSTRATA_STUDIO_CODEX_BIN") or
                str(home / "tools/codex/bin/codex")}


class Guide:
    def __init__(self, home, framework, *, model_path=None):
        self.state_dir, self.framework = home, framework
        self.model_path = Path(model_path) if model_path else home / "models.json"
        self.model_environment = ({"AGENTSTRATA_LLM_CONFIG": str(self.model_path)} if model_path else studio_environment(home))
        self.process = None
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return
            if not self.model_path.is_file():
                return
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            token = secrets.token_urlsafe(48)
            env = {**os.environ, **self.model_environment, "PYTHONPATH": str(self.framework / "src"),
                   "CHATCOPILOT_SOURCE_ROOT": str(self.framework),
                   "CHATCOPILOT_GATEWAY_STATE_ROOT": str(private_directory(self.state_dir / "gateway")),
                   "CHATCOPILOT_WORKSPACE_ROOT": str(private_directory(self.state_dir / "guide-workspace")),
                   "CHATCOPILOT_GATEWAY_PORT": str(port), "AGENTSTRATA_STUDIO_GATEWAY_TOKEN": token,
                   "CHATCOPILOT_INSTANCE_ID": "studio-guide"}
            with (self.state_dir / "guide.log").open("ab") as log:
                self.process = subprocess.Popen([sys.executable, "-m", "chatcopilot", "run", "--bot",
                    str(self.framework / "bots/studio-guide/bot.yaml")], cwd=self.framework, env=env,
                    stdout=log, stderr=log)
            write_private(self.state_dir / "guide.json", json.dumps({"url":f"ws://127.0.0.1:{port}", "token":token}))

    def stop(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=30)
            self.process = None


def routes(home, framework, guide):
    router = APIRouter()
    store = ModelSettingsStore(home / "models.json")

    @router.get("/api/setup")
    def status():
        from chatcopilot.external_tools.codex_cli.auth_cli import status_lanes
        view = store.read()
        return {"configured": bool(view["bindings"].get("chat")), "models":{
            k: v["model"] for k,v in view["profiles"].items()},
            "auth": [row.to_dict() for row in status_lanes(home / "auth", "all")]}

    @router.post("/api/setup")
    def configure(body: Models):
        current = store.view()
        bindings = {"chat":"application", "research":"application", "evaluation.judge":"application", "harness":"worker"}
        worker = {"kind":"codex", "auth":{"mode":"chatgpt", "profile":"worker"}}
        connection = {"kind":"codex", "auth":{"mode":"chatgpt","profile":"main"}}
        if not body.chatgpt:
            if not body.endpoint.strip():
                raise ValueError("请填写聊天模型的 API 地址")
            credentials = home / ("model-" + hashlib.sha256(body.endpoint.strip().encode()).hexdigest()[:16] + ".env")
            connection = {"kind":"openai_compatible", "base_url":body.endpoint.strip(),
                          "auth":{"mode":"api_key","key_env":"AGENTSTRATA_STUDIO_API_KEY"}, "env_file":str(credentials)}
            if any(ord(c) < 32 for c in body.key) or not (body.key or credentials.is_file()):
                raise ValueError("请填写本连接的 API Key")
        data = {"connections":{"chat":connection, "worker":worker},
                "profiles":{"application":{"connection":"chat","model":body.model},
                            "worker":{"connection":"worker","model":body.worker}}, "bindings":bindings}
        if not body.worker.strip():
            data["connections"].pop("worker")
            data["profiles"].pop("worker")
            data["bindings"].pop("harness")
        def credential(_new, _old):
            if not body.chatgpt and body.key:
                write_private(credentials, "AGENTSTRATA_STUDIO_API_KEY=" + json.dumps(body.key) + "\n")
        store.save(data, revision=current["revision"], validate=credential)
        guide.stop()
        guide.start()
        return {"ok":True}

    @router.post("/api/setup/login/{lane}")
    def login(lane: str):
        if lane not in {"main", "worker"}:
            raise ValueError("未知授权类型")
        import pwd
        from studio.delivery import windows_path
        distro = os.environ.get("WSL_DISTRO_NAME")
        if not distro:
            raise ValueError("请从 Windows 的开发启动器打开账号授权")
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        windows_path(framework / "studio/windows/login.ps1"), "-Distro", distro,
                        "-User", pwd.getpwuid(os.getuid()).pw_name, "-Framework", str(framework), "-Python", sys.executable, "-StudioHome", str(home),
                        "-Lane", lane], check=True, timeout=30)
        return {"message":"已打开本机授权窗口，请按窗口提示在浏览器完成登录。"}
    return router
