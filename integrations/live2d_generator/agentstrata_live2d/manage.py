"""Driver for the existing AgentStrata plugin manager. Heavy packages load only after prepare."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
from importlib.resources import files
import importlib.util
import json
from pathlib import Path
import sys
from urllib.request import urlopen

from .config import Config, load_environment, source_notice
from .repository import private_directory, write_bytes, write_json


def resource(name: str) -> Path:
    return Path(str(files(__package__).joinpath("resources", name)))


def prepare(request: dict) -> dict:
    from chatcopilot.core.install_artifacts import run_install
    from chatcopilot.plugins.runtime import require_free_port

    root = Path(request["root"])
    target = root / "config/generator.json"
    config = Config.load(target) if target.is_file() else Config.parse(request.get("options", {}))
    require_free_port(config.port)
    for folder in ("config", "data/jobs", "logs", "runtime"):
        private_directory(root / folder)
    lock = resource("requirements.lock")
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    marker = root / "runtime/dependency-sha256"
    if not marker.is_file() or marker.read_text() != digest:
        for name in ("build.lock", "requirements.lock"):
            run_install([request["uv"], "pip", "install", "--python", sys.executable, "--no-config",
                         "--require-hashes", "--no-build-isolation", "-r", str(resource(name))],
                        log=root / "logs/dependencies.log")
        marker.write_text(digest)
    if not target.exists():
        write_json(target, asdict(config))
    else:
        config = Config.load(target)
    environment = root / "config/runtime.env"
    lines = (environment.read_text() if environment.exists() else resource(".env.example").read_text()).splitlines()
    lines = [line for line in lines if not line.startswith("CHATCOPILOT_SOURCE_URL=")]
    lines.append("CHATCOPILOT_SOURCE_URL=" + json.dumps(request["host"]["source_url"]))
    write_bytes(environment, ("\n".join(lines) + "\n").encode())
    return {"url": f"http://localhost:{config.port}", "observable_agents": [],
            "provenance": {"requirements_sha256": digest, "source_url": request["host"]["source_url"]},
            "services": [{"name": "studio", "command": [str(root / "runtime/venv/bin/python"),
                "-m", "agentstrata_live2d.cli", "serve", "--config", str(target),
                "--root", str(root / "data/jobs")], "cwd": str(root),
                "environment_file": str(environment), "port": config.port,
                "ready_url": f"http://127.0.0.1:{config.port}/health"}]}


def check(request: dict, *, online: bool = False) -> dict:
    root = Path(request["root"])
    config = Config.load(root / "config/generator.json")
    load_environment(root / "config/runtime.env")
    marker = root / "runtime/dependency-sha256"
    checks = {"python": sys.version_info[:2] == (3, 12),
              "source_configured": source_notice()["source_configured"],
              "dependencies": marker.is_file() and marker.read_text() == hashlib.sha256(resource("requirements.lock").read_bytes()).hexdigest(),
              "packages": all(importlib.util.find_spec(p) is not None for p in
                              ("image2live2d", "PIL", "psd_tools", "numpy", "fastapi", "uvicorn", "gradio_client"))}
    if online:
        try:
            with urlopen(f"http://127.0.0.1:{config.port}/health", timeout=5) as response:
                checks["studio"] = json.loads(response.read(1024)) == {"ok": True}
        except Exception:
            checks["studio"] = False
    return {"ok": all(checks.values()), "checks": checks, "capabilities": config.capabilities()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "check", "doctor"))
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request = json.loads(args.request.read_text())
        result = prepare(request) if args.operation == "prepare" else check(request, online=args.operation == "doctor")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"{args.operation} failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
