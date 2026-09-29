"""Discovery adapters for the same connections used by runtime hosts."""
from __future__ import annotations

import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Mapping

from chatcopilot.contracts.model_runtime import digest
from chatcopilot.core.model_credentials import CredentialError, credential_status, validate_auth_root_path, credential_lease
from chatcopilot.core.model_settings import connection_environment
from chatcopilot.external_tools.codex_cli.app_server import AppServerProcess
from chatcopilot.external_tools.codex_cli.command import build_codex_subprocess_env


def connection_identity(connection: dict, environment: Mapping[str, str]) -> str:
    env = connection_environment(connection, environment)
    auth = connection["auth"]
    if auth["mode"] == "api_key":
        return digest({"key": env.get(auth["key_env"], "")})
    root = env.get(connection.get("credential_root_env", "CHATCOPILOT_CODEX_BOT_HOME"), "")
    try:
        status = credential_status(Path(root), auth["profile"])
        identity = {"root": root, "lane": auth["profile"], "generation": status.generation,
                    "state": status.state}
    except (ValueError, OSError, CredentialError):
        identity = {"root": root, "lane": auth["profile"], "state": "unavailable"}
    binary = env.get(connection.get("codex_bin_env", "CHATCOPILOT_CODEX_BIN"), "")
    try:
        stat = Path(binary).stat()
        identity["binary"] = [binary, stat.st_mtime_ns, stat.st_size]
    except OSError:
        identity["binary"] = binary
    return digest(identity)


def discover_models(connection: dict, environment: Mapping[str, str], repository: Path) -> list[dict]:
    env = connection_environment(connection, environment)
    if connection["kind"] == "codex":
        return codex_models(connection, env, repository)
    import requests
    key = env.get(connection["auth"]["key_env"], "")
    if not key:
        raise ValueError("API 凭据未配置")
    base = connection.get("base_url") or "https://api.openai.com/v1"
    response = requests.get(base.rstrip("/") + "/models", headers={"Authorization": "Bearer " + key},
                            timeout=min(connection.get("timeout", 120), 20), allow_redirects=False)
    response.raise_for_status()
    rows = response.json().get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("模型目录格式无效")
    return rows


def codex_models(connection: dict, environment: Mapping[str, str], repository: Path) -> list[dict]:
    binary = Path(environment.get(connection.get("codex_bin_env", "CHATCOPILOT_CODEX_BIN"), "")).resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Codex 可执行文件不可用")
    with binary.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise ValueError("Codex 需要 Linux 原生可执行文件")
    with tempfile.TemporaryDirectory(prefix="as-model-") as temporary, ExitStack() as stack:
        home = Path(temporary) / "codex-home"
        auth = connection["auth"]
        if auth["mode"] == "chatgpt":
            root = validate_auth_root_path(environment.get(connection.get("credential_root_env", "CHATCOPILOT_CODEX_BOT_HOME"), ""))
            stack.enter_context(credential_lease(root, auth["profile"], home, blocking=False))
        else:
            home.mkdir(mode=0o700)
        command = [str(binary), "app-server", "--listen", "stdio://", "--strict-config"]
        for item in ("project_doc_max_bytes=0", "mcp_servers={}", "features.hooks=false", "features.apps=false",
                     "features.image_generation=false", "features.multi_agent=false"):
            command.extend(["--config", item])
        with AppServerProcess(command, cwd=repository,
                env=build_codex_subprocess_env(str(binary), runtime_home=home), timeout_seconds=20,
                on_notification=lambda *_: None, on_poll=lambda: None) as rpc:
            rpc.initialize()
            if auth["mode"] == "api_key":
                key = environment.get(auth["key_env"], "")
                if not key:
                    raise ValueError("API 凭据未配置")
                rpc.request("account/login/start", {"type": "apiKey", "apiKey": key})
            rows, seen = [], set()
            cursor = None
            while True:
                result = rpc.request("model/list", {"limit": 100, "includeHidden": True,
                    **({"cursor": cursor} if cursor else {})})
                page = result.get("data")
                if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                    raise ValueError("模型目录格式无效")
                rows.extend(page)
                cursor = result.get("nextCursor")
                if cursor is None:
                    return rows
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ValueError("模型目录游标无效")
                seen.add(cursor)
