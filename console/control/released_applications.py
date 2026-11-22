"""Attach a completed Studio service release to another local workbench."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

import yaml

from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_directory, private_file, private_lock
from studio import installer
from studio.installer.containers import identifier


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise KeyError("无效的 Studio 项目或构建编号")
    return value


def _versions(home, project):
    root = home / "releases" / _id(project)
    records = []
    for path in root.glob("*/release.json"):
        _id(path.parent.name)
        private_directory(path.parent, create=False)
        private_file(path)
        record = json.loads(path.read_text())
        if record["project"] != project or record["build"] != path.parent.name:
            raise ValueError("接入记录的应用或版本身份不一致")
        records.append(record)
    return sorted(records, key=lambda record: record["updated"], reverse=True)


def applications(apps):
    for root in sorted((apps.state_dir / "releases").glob("*")):
        if re.fullmatch(r"[0-9a-f]{32}", root.name):
            records = _versions(apps.state_dir, root.name)
            if records:
                version = records[0]
                row = apps._view("studio:" + root.name, version["name"], "source", "release", project=root.name)
                row.update(plan_state="ready", icon=version["icon"], last_activity=version["updated"])
                yield row


def catalog(apps, row):
    versions = _versions(apps.state_dir, row["project_id"])
    builds = []
    for record in versions:
        receipt = record["receipt"]
        for platform, info in receipt["installers"].items():
            builds.append({"id": "project-" + record["build"], "platform": platform, "state": "ready",
                           "origin": receipt["origin"], "docker": receipt["docker"], "components": receipt["components"],
                           "requirements": receipt["requirements"], "updated": record["updated"], **info,
                           "installation": record["installations"].get(platform), "acceptance": record["accepted"]})
    available = {item["platform"] for item in builds}
    return {"builds": builds, "platforms": [platform for platform in installer.FILES if platform in available],
            "docker": True, "requirements": versions[0]["receipt"]["requirements"], "commit": "", "reason": ""}


def artifact(apps, row, key, platform):
    record = next((r for r in _versions(apps.state_dir, row["project_id"]) if "project-" + r["build"] == key), None)
    if not record or platform not in record["receipt"]["installers"]:
        raise KeyError(key)
    file = apps.state_dir / "releases" / record["project"] / record["build"] / installer.FILES[platform]
    private_file(file)
    return file, record["receipt"]["installers"][platform]["sha256"]


def _web_entry(file, receipt):
    # Only read the two declarations from the SHA-verified installer. No extraction or execution.
    declarations = {}
    with file.open("rb") as stream:
        for line in iter(lambda: stream.readline(65536), b""):
            if line == b"__APPLICATION_PAYLOAD__\n":
                break
            if not line.endswith(b"\n"):
                raise ValueError("无法读取安装包载荷")
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            for member in archive:
                if member.name in {"compose.yaml", "install.json"} and member.isfile():
                    declarations[member.name] = archive.extractfile(member).read()
                if len(declarations) == 2:
                    break
    settings = json.loads(declarations["install.json"])
    if settings["application"] != receipt["application"] or settings["origin"] != receipt["origin"]:
        raise ValueError("包内应用与已验收构建不匹配")
    entry = settings.get("entry")
    if not isinstance(entry, dict):
        raise ValueError("该服务安装包未声明网页入口")
    service = entry.get("service")
    port = entry.get("port")
    if not identifier(service) or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("安装包的服务或端口无效")
    services = yaml.safe_load(declarations["compose.yaml"])["services"]
    image = settings["images"][services[service]["image"]]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("安装包缺少可验证的本机网页入口")
    return {"service": service, "port": port, "image": image}


def save_release(home, row, build, receipt, acceptance, records, sources):
    project = _id(row["project_id"])
    _id(build)
    private_directory(home)
    root = private_directory(private_directory(home / "releases") / project)
    final = root / build
    with private_lock(root / "attach.lock", timeout=10), tempfile.TemporaryDirectory(dir=root) as tmp:
        staged = Path(tmp) / "release"
        staged.mkdir(mode=0o700)
        for platform, info in receipt["installers"].items():
            src, digest = sources[platform]
            dst = staged / installer.FILES[platform]
            if src.is_symlink() or not src.is_file():
                raise ValueError("来源安装包不是普通文件")
            shutil.copyfile(src, dst)
            dst.chmod(0o600)
            with dst.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest or actual != info["sha256"] or dst.stat().st_size != info["size"]:
                raise ValueError("接入时安装包 SHA-256 或大小校验失败")
        record = {"project": project, "build": build, "name": row["name"], "icon": row.get("icon", "box"),
                  "updated": records[0]["updated"], "accepted": acceptance, "receipt": receipt,
                  "entry": _web_entry(staged / installer.FILES[installer.LINUX], receipt),
                  "installations": {r["platform"]: r["installation"] for r in records if r.get("installation")}}
        if final.exists():
            existing = next(r for r in _versions(home, project) if r["build"] == build)
            if existing["receipt"] != receipt or existing["accepted"] != acceptance:
                raise ValueError("同一版本已有不同的交付回执")
            for platform, info in receipt["installers"].items():
                file = final / installer.FILES[platform]
                private_file(file)
                with file.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != info["sha256"]:
                        raise ValueError("目标工作台已有文件的摘要不匹配")
        else:
            write_private(staged / "release.json", json.dumps(record, ensure_ascii=False, sort_keys=True))
            staged.rename(final)
    return row["id"]


def _docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=5).stdout.strip()


def open_url(apps, row):
    record = _versions(apps.state_dir, row["project_id"])[0]
    entry = record["entry"]
    project = "as-" + record["receipt"]["application"]
    try:
        ids = _docker("ps", "-q", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project,
                     "--filter", "label=com.docker.compose.service=" + entry["service"]).splitlines()
        if len(ids) == 1:
            fmt = '{{json .State.Running}} {{.Image}} {{json .NetworkSettings.Ports}}'
            running, image, ports = _docker("inspect", "--type", "container", "--format", fmt, ids[0]).split(" ", 2)
            if running == "true" and image == entry["image"]:
                for binding in (json.loads(ports) or {}).get(str(entry["port"]) + "/tcp") or []:
                    port = int(binding["HostPort"])
                    if binding["HostIp"] == "127.0.0.1" and 1 <= port <= 65535:
                        return f"http://127.0.0.1:{port}/"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError("当前无法读取本机应用的运行入口") from exc
    raise ValueError("未找到此已验收版本正在运行的本机网页入口")
