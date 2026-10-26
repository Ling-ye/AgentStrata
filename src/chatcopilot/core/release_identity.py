"""Content and process identities used by the local managed-version launcher."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import sys
import shutil
import subprocess
import ipaddress

from chatcopilot.contracts.model_runtime import digest
from chatcopilot.core.application_configuration import load_application_spec, read_configuration_yaml
from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.private_sqlite import private_file


def read_json(path: Path) -> dict:
    private_file(path)
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Version record must be an object")
    return value


def tree_manifest(root: Path) -> dict:
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Version component must be a canonical directory")
    rows = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ValueError("Version components cannot contain symlinks")
        if path.is_file():
            rows[str(path.relative_to(root))] = trusted_source_sha256(path, root=root, max_bytes=max(1, path.stat().st_size))
    return rows


def configuration_manifest(bot: Path) -> dict:
    """Public application/profile references; credential environment files stay outside."""
    app = load_application_spec(bot)
    root = app.source_path.parent
    paths = {app.source_path, app.agent_profile.source_path}
    def references(value):
        if isinstance(value, dict):
            for item in value.values():
                references(item)
        elif isinstance(value, list):
            for item in value:
                references(item)
        elif isinstance(value, str) and value:
            relative = Path(value)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Activation requires public configuration references inside the application directory")
            path = root / relative
            if not path.is_file():
                raise ValueError("A declared public configuration reference is missing")
            paths.add(path)
    references(app.agent_profile.raw.get("prompts", {}))
    mcp = app.agent_profile.raw.get("tools", {}).get("mcp", {}).get("servers")
    if mcp:
        references(mcp)
        declaration = read_configuration_yaml(root / mcp)
        for server in declaration.get("servers", []):
            if server.get("env_file") and not Path(server["env_file"]).expanduser().is_absolute():
                raise ValueError("Private MCP environment files need absolute references for activation")
    result = {}
    for path in sorted(paths):
        content_hash = trusted_source_sha256(path, root=root, max_bytes=max(1, path.stat().st_size))
        if path.suffix in {".yaml", ".yml"}:
            value = read_configuration_yaml(path)
            content_hash = digest(value)
        result[str(path.relative_to(root))] = content_hash
    return result


def execution_identity(bot: Path, environment: dict[str, str]) -> dict:
    from chatcopilot.core.model_settings import read_settings, resolve_binding
    profile = load_application_spec(bot).agent_profile.raw
    binding = profile.get("llm", {}).get("chat", {}).get("binding", "chat")
    config = resolve_binding(binding, document=read_settings(environment), environment=environment)
    runtime = environment_identity()
    if profile.get("agents", {}).get("runtime", "langgraph") == "codex":
        located = config.codex_bin or shutil.which("codex", path=environment.get("PATH")) or environment.get("CHATCOPILOT_CODEX_BIN") or environment.get("CODEX_BIN") or ""
        binary = Path(located)
        if not binary.is_file():
            raise ValueError("Activation requires an explicit native Codex executable binding")
        with binary.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                raise ValueError("Activation requires the native Linux Codex executable")
            stream.seek(0)
            runtime["codex_executable_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"model": selected_model_identity(profile.get("agents", {}).get("runtime", "langgraph"), config), "environment": runtime}


def selected_model_identity(runtime_id: str, config) -> str:
    value = asdict(config)
    value.pop("api_key", None)
    # Credentials remain mutable private references. Rotation is not a new
    # model choice and must not make a previously enabled version unbootable.
    return digest({"runtime": runtime_id, "model": value})


def model_identity(bot: Path, environment: dict[str, str]) -> str:
    return execution_identity(bot, environment)["model"]


def environment_identity() -> dict:
    packages = {}
    for name in ("langgraph", "langgraph-checkpoint", "mcp", "websockets", "uvicorn", "deepeval"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python": sys.version.split()[0], "platform": platform.system(), "machine": platform.machine(), "packages": packages}


def probe_environment(python: str) -> dict:
    names = tuple(environment_identity()["packages"])
    program = ("import json,sys,platform\nfrom importlib.metadata import version,PackageNotFoundError\np={}\n"
        f"for name in {names!r}:\n try:p[name]=version(name)\n except PackageNotFoundError:p[name]=None\n"
        "print(json.dumps(dict(python=sys.version.split()[0],platform=platform.system(),machine=platform.machine(),packages=p)))")
    result = subprocess.run([python, "-I", "-c", program], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def process_identity(pid: int) -> dict | None:
    try:
        path = Path(f"/proc/{pid}")
        if path.stat().st_uid != os.getuid():
            return None
        raw = (path / "stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "start_ticks": int(fields[19]),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (OSError, ValueError, IndexError):
        return None


def owns_listener(pid: int, port: int) -> bool:
    try:
        sockets = set()
        for path in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(path)
            except FileNotFoundError:
                continue
            if target.startswith("socket:["):
                sockets.add(target[8:-1])
        for name in ("tcp", "tcp6"):
            for line in Path(f"/proc/{pid}/net/{name}").read_text().splitlines()[1:]:
                fields = line.split()
                address, local_port = fields[1].split(":")
                raw = bytes.fromhex(address)
                packed = b"".join(raw[index:index + 4][::-1] for index in range(0, len(raw), 4))
                if (fields[3] == "0A" and int(local_port, 16) == port and fields[9] in sockets
                        and ipaddress.ip_address(packed).is_loopback):
                    return True
    except (OSError, ValueError, IndexError):
        return False
    return False
