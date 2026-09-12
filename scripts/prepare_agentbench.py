#!/usr/bin/env python3
"""Prepare the pinned AgentBench FC DB/OS environment; never invoke a model."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlunsplit

import requests
import yaml

PROJECT = "agentstrata-agentbench"
TASKS = {"dbbench": "dbbench-std", "os": "os-std"}
BENCH_REV = "d1e4a10db08c87075c78972e48ecc182be03e2d5"
RL_REV = "6a73409d31ba695d383b978a8ad3ef400d90c054"
SOURCES = (
    ("AgentBench", BENCH_REV, "cfdb8f02129318d7e524dd39e349b196e9e6e8fe676a3dae2230e3c1314684b8"),
    ("AgentRL", RL_REV, "8fe53d5a1362bc582be8691c782d7349f7245747fa95758961e914b306b2cb39"),
)
CONTROLLER = "jingbh/agentrl-controller@sha256:8d271c890fde9fd6b5035e736b8015c45035fb9ba72fbb49f26a7c31bd1dc9b7"
WORKER = "agentstrata-agentbench-worker:db-os-v1"
OS_IMAGES = "agentstrata-agentbench-os-v1"
TEMPLATES = Path(__file__).resolve().parents[1] / "deploy" / "evals" / "agentbench"


def command(args: list[str], *, cwd: Path | None = None, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else ""


def compose(root: Path, *args: str, capture: bool = False) -> str:
    return command(["docker", "compose", "-p", PROJECT, "-f", str(root / "compose.yaml"), *args], capture=capture)


def extract_source(root: Path, context: Path, name: str, revision: str, digest: str) -> Path:
    archive = root / "upstream" / f"{name}-{revision}.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.is_file():
        response = requests.get(f"https://github.com/THUDM/{name}/archive/{revision}.tar.gz", timeout=120)
        response.raise_for_status()
        temporary = archive.with_suffix(".part")
        temporary.write_bytes(response.content)
        temporary.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError(f"{name} archive differs from its pinned checksum")
    with tarfile.open(archive) as bundle:
        bundle.extractall(context, filter="data")
    target = context / name
    (context / f"{name}-{revision}").rename(target)
    return target


def local_config(source: dict, *, os_task: bool) -> dict:
    params = source["default"]["parameters"]
    params["concurrency"] = 1
    params["env_options"]["network_name"] = PROJECT + "_env"
    params["env_options"]["state_options"]["connection"] = {"host": "redis", "port": 6379}
    if os_task:
        params["docker_config"]["localhost"] = OS_IMAGES
    return source


def build(root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="build-", dir=root) as temporary:
        context = Path(temporary)
        for args in SOURCES:
            extract_source(root, context, *args)
        config = context / "configs"
        config.mkdir()
        for name in TASKS:
            document = yaml.safe_load((context / "AgentBench" / "configs" / "tasks" / f"{name}.yaml").read_text())
            (config / f"{name}.yaml").write_text(yaml.safe_dump(local_config(document, os_task=name == "os"), sort_keys=False))
        for name in ("worker.Dockerfile", "configure_image.py", "export_catalog.py"):
            shutil.copyfile(TEMPLATES / name, context / name)
        command(["docker", "build", "--progress", "plain", "-f", str(context / "worker.Dockerfile"), "-t", WORKER, str(context)])
        dockerfiles = context / "AgentBench" / "data" / "os_interaction" / "res" / "dockerfiles"
        for flavor in ("default", "packages", "ubuntu"):
            path = dockerfiles / flavor
            source = path.read_text()
            if not source.startswith("FROM docker.1ms.run/ubuntu\n"):
                raise ValueError("pinned OS base image declaration differs")
            path.write_text(source.replace("FROM docker.1ms.run/ubuntu\n", "FROM ubuntu:24.04\n", 1))
            command(["docker", "build", "--progress", "plain", "-f", str(path), "-t", f"{OS_IMAGES}/{flavor}", str(dockerfiles)])
    for image in (CONTROLLER, "redis:7", "mysql:8"):
        command(["docker", "pull", image])


def compose_document(port: int) -> dict:
    services = {
        "controller": {"image": CONTROLLER, "command": ["controller"],
                       "ports": [f"127.0.0.1:{port}:5020"], "mem_limit": "256m"},
        "redis": {"image": "redis:7", "command": ["redis-server", "--save", "", "--appendonly", "no"],
                  "mem_limit": "128m"},
    }
    for service, task in TASKS.items():
        services[service] = {
            "image": WORKER, "mem_limit": "512m", "cpus": 1,
            "command": ["--config", f"configs/{service}.yaml", "--controller", urlunsplit(("http", "controller:5020", "/api", "", "")),
                        "--self", urlunsplit(("http", f"{service}:5021", "/api", "", "")), task],
            "volumes": ["/var/run/docker.sock:/var/run/docker.sock"],
            "depends_on": ["controller", "redis"],
        }
    for service in services.values():
        service.update(restart="unless-stopped", networks=["env"], labels={"agentstrata.component": "agentbench-fc"})
    return {"name": PROJECT, "services": services, "networks": {"env": {"name": PROJECT + "_env"}}}


def inspect_workers(url: str) -> dict:
    with requests.Session() as client:
        client.trust_env = False
        response = client.get(url + "/list_workers", timeout=(3, 5), allow_redirects=False)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("controller worker response is invalid")
        return value


def export_catalog(root: Path, workers: dict) -> dict:
    rows = []
    for service, task in TASKS.items():
        output = compose(root, "exec", "-T", service, "python", "export_catalog.py", task, BENCH_REV, capture=True)
        exported = [json.loads(line) for line in output.splitlines() if line.strip()]
        if {r["index"] for r in exported} != set(workers[task]["indices"]):
            raise ValueError(f"{task} exported indices differ from running worker")
        rows.extend(exported)
    path = root / "tasks.jsonl"
    temporary = root / "tasks.jsonl.tmp"
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temporary.replace(path)
    images = {}
    for image in (CONTROLLER, WORKER, "redis:7", "mysql:8", *(f"{OS_IMAGES}/{kind}" for kind in ("default", "packages", "ubuntu"))):
        images[image] = command(["docker", "image", "inspect", image, "--format", "{{.Id}}"], capture=True).strip()
    return {"data_path": str(path), "source_revision": BENCH_REV, "agentrl_revision": RL_REV,
            "data_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "case_counts": dict(Counter(r["task"] for r in rows)),
            "images": images, "environment_profile": "DB buffer 128MiB; task concurrency 1; OS Ubuntu 24.04; native upstream task/scoring code"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "status", "stop"))
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("CHATCOPILOT_EVALS_DATA_DIR", "~/.cache/agentstrata/evals")).expanduser() / "agentbench-fc")
    parser.add_argument("--port", type=int, default=15020)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    docker_config = root / "docker-config"
    docker_config.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["DOCKER_CONFIG"] = str(docker_config)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be in 1024..65535")
    url = f"http://127.0.0.1:{args.port}/api"
    if args.action == "status":
        workers = inspect_workers(url)
        print(json.dumps({task: {"cases": len(v.get("indices", [])), "workers": v.get("workers", {})} for task, v in workers.items()}, ensure_ascii=False, indent=2))
        return
    if args.action == "stop":
        compose(root, "stop")
        return
    root.mkdir(parents=True, exist_ok=True)
    command(["docker", "info", "--format", "{{.ServerVersion}}"])
    build(root)
    (root / "compose.yaml").write_text(yaml.safe_dump(compose_document(args.port), sort_keys=False))
    compose(root, "up", "-d")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            workers = inspect_workers(url)
            if all(task in workers and any(w.get("status") == "ALIVE" for w in workers[task].get("workers", {}).values()) for task in TASKS.values()):
                break
        except requests.RequestException:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("AgentBench workers did not become ready; inspect the dedicated Compose logs")
    receipt = {"ready": True, "controller_url": url, **export_catalog(root, workers)}
    (root / "source.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
