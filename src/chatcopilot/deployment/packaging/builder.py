"""Build one application payload from a frozen declaration and source manifest."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Callable, Mapping, Sequence

from chatcopilot.core.source_snapshot import copy_sources
from . import containers
from .plan import FILES, LINUX, PackagePlan
from .source import LEGAL_FILES, copy_public


BUILD_RECEIPT_SCHEMA = "agentstrata-build-receipt/v1"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build(source: Path, dest: Path, *, bot: str, name: str, manifest: Mapping, origin: Mapping,
          plan: PackagePlan, targets: Sequence[str] | None = None,
          context_provider: containers.ContextProvider | None = None,
          docker_runner: containers.DockerRunner = containers.docker,
          nsis: Callable[[Path], None] | None = None,
          accepted_source_digest: str | None = None) -> dict:
    """Prepare a generic application payload, then delegate OS envelopes to formats."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", bot):
        raise ValueError("应用标识或安装平台无效")
    if not name.strip() or not name.isprintable():
        raise ValueError("应用名称无效")
    if not isinstance(origin, Mapping) or not isinstance(origin.get("revision"), str):
        raise ValueError("安装包来源无效")
    if accepted_source_digest is not None and (origin.get("kind") != "candidate"
            or not re.fullmatch(r"[a-f0-9]{64}", accepted_source_digest)
            or accepted_source_digest != origin["revision"]):
        raise ValueError("独立接受摘要与安装包来源不匹配")
    if plan.recipe == "native":
        raise ValueError("原生应用须由宿主配方构建")
    targets = plan.select_targets(targets)
    dest.mkdir(parents=True, exist_ok=False)
    prefix = plan.prefix
    with tempfile.TemporaryDirectory(dir=dest) as temporary:
        stage = Path(temporary)
        payload = stage / "payload"
        payload.mkdir(mode=0o700)
        if plan.docker:
            config = containers.prepare(source, manifest, bot, stage, payload, origin["revision"], plan.spec,
                                        prefix=prefix,
                                        context_provider=context_provider, docker_runner=docker_runner)
        elif plan.recipe == "client":
            copy_public(source, manifest, prefix, payload / "source")
            config = {}
        elif plan.recipe == "static":
            copy_public(source, manifest, prefix, payload / "site")
            if not (payload / "site/index.html").is_file():
                raise ValueError("已选源码缺少应用入口 index.html")
            config = {"entry": "site/index.html"}
        else:
            raise ValueError("此应用缺少通用安装入口")
        installed = {}
        for platform in targets:
            if platform not in plan.clients:
                continue
            target = "source/" + plan.clients[platform]
            path = payload / target
            if not path.is_file() or (platform == LINUX and path.suffix.lower() not in {".htm", ".html"}
                                      and not path.stat().st_mode & 0o111):
                raise ValueError("所选源码中的客户端入口缺失或不可执行")
            installed[platform] = target
        if plan.clients:
            config["clients"] = installed
            if LINUX in installed:
                (payload / "client-linux").write_text(installed[LINUX] + "\n")
        for legal in LEGAL_FILES:
            if legal in manifest:
                copy_sources(source, payload, {legal: manifest[legal]})
        components = [*config.get("images", {})]
        if plan.docker:
            components.append("Docker Compose 部署配置")
        if plan.clients:
            components.extend(f"{platform} 客户端 · {entry}" for platform, entry in installed.items())
        elif plan.recipe == "static":
            components.append("网页及静态资源 · site/index.html")
        config.update(application=bot, name=name, origin=dict(origin), docker=plan.docker,
                      requirements=list(plan.requirements))
        (payload / "install.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        from .formats import write_installers
        write_installers(payload, stage, dest, bot=bot, targets=targets, docker=plan.docker, nsis=nsis)
    receipt = {"schema": BUILD_RECEIPT_SCHEMA, "recipe": plan.recipe, "source_directory": plan.prefix.rstrip("/") or ".",
               "application": bot, "origin": dict(origin), "docker": plan.docker,
               "requirements": list(plan.requirements), "components": components,
               "installers": {platform: {"sha256": digest(dest / FILES[platform]),
                                           "size": (dest / FILES[platform]).stat().st_size}
                              for platform in targets}}
    if plan.descriptor is not None:
        receipt["declaration_sha256"] = manifest[plan.descriptor]["sha256"]
    if plan.docker:
        receipt["images"] = dict(config["images"])
    if accepted_source_digest is not None:
        receipt["accepted_source_digest"] = accepted_source_digest
    (dest / "build-receipt.json").write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    return receipt
