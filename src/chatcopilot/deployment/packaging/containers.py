"""Validate declared Compose applications and freeze their exact image inputs."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from typing import Callable, Mapping

import yaml

from .source import copy_public


ContextProvider = Callable[[Path, Mapping, str, Path, Mapping, str], tuple[dict, dict, dict, bool]]
DockerRunner = Callable[..., str]


@dataclass(frozen=True)
class _LocalImage:
    directory: Path
    recipe: str


@dataclass(frozen=True)
class _RegistryImage:
    reference: str
    license_file: Path


def docker(*args, cwd=None):
    try:
        return subprocess.run(["docker", *args], cwd=cwd, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=1800).stdout.strip()
    except FileNotFoundError:
        raise ValueError("构建端需要可用的 Docker Engine") from None
    except subprocess.SubprocessError:
        raise ValueError(f"Docker {args[0]} 失败，请核对构建端的引擎、依赖来源及应用镜像配置") from None


def identifier(name):
    return isinstance(name, str) and re.fullmatch("[a-z][a-z0-9-]*", name)


def declared_context(source: Path, manifest: Mapping, bot: str, context: Path, spec: Mapping, prefix: str):
    """Read an application's own Compose declaration from its frozen public projection."""
    copy_public(source, manifest, prefix, context)
    if not (context / "compose.yaml").is_file() or not (context / "install.json").is_file():
        raise ValueError("请在应用开发目录补齐 compose.yaml 和 install.json，再生成服务型安装包")
    declaration = yaml.safe_load((context / "compose.yaml").read_text())
    if not isinstance(declaration, dict) or set(declaration) - {"services", "volumes"}:
        raise ValueError("安装包 Compose 只接受本应用的 services 和命名 volumes")
    volumes = declaration.get("volumes") or {}
    if not isinstance(volumes, dict) or any(value not in (None, {}) or not identifier(name)
                                            for name, value in volumes.items()):
        raise ValueError("服务数据须使用本应用的命名卷")
    entry = spec.get("entry")
    if entry is not None and (not isinstance(entry, dict) or set(entry) != {"service", "port"}
                              or not identifier(entry.get("service"))
                              or type(entry.get("port")) is not int or not 1 <= entry["port"] <= 65535):
        raise ValueError("网页入口需指定 Compose 服务和容器端口")
    return declaration, {"entry": entry}, spec.get("image_licenses", {}), False


def _resolve_image_input(service: Mapping, context: Path, notice: str | None) -> _LocalImage | _RegistryImage:
    build = service.get("build")
    image = service.get("image")
    if build is not None and image is None:
        recipe = "Dockerfile"
        if isinstance(build, dict):
            if set(build) - {"context", "dockerfile"}:
                raise ValueError("应用服务构建只接受本地 context 和 dockerfile")
            recipe = build.get("dockerfile", recipe)
            build = build.get("context", ".")
        if any(not isinstance(value, str) or not value or "\\" in value or PurePosixPath(value).is_absolute()
               or ".." in PurePosixPath(value).parts for value in (build, recipe)):
            raise ValueError("Docker 构建目录和配方须位于本应用公开源码内")
        directory = context / build
        dockerfile = directory / recipe
        if not dockerfile.is_file() or not dockerfile.resolve().is_relative_to(context.resolve()):
            raise ValueError("应用服务缺少本地 Dockerfile")
        return _LocalImage(directory, recipe)
    if isinstance(image, str) and build is None and re.fullmatch(r"[\w./:-]+@sha256:[0-9a-f]{64}", image):
        if (not isinstance(notice, str) or Path(notice).is_absolute() or ".." in Path(notice).parts
                or not (context / notice).is_file()):
            raise ValueError("随包第三方镜像须固定摘要并提供再分发许可说明")
        return _RegistryImage(image, context / notice)
    raise ValueError("每个服务必须指定一个构建目录或固定摘要的可分发镜像")


def prepare(source: Path, manifest: Mapping, bot: str, stage: Path, payload: Path, version: str,
            spec: Mapping, *, prefix: str, context_provider: ContextProvider | None = None,
            docker_runner: DockerRunner = docker) -> dict:
    """Build or fetch all declared images, then emit a self-contained Compose payload."""
    context = stage / "context"
    provider = context_provider or declared_context
    compose, config, licenses, builtin = provider(source, manifest, bot, context, spec, prefix)
    for path in [context, *context.rglob("*")]:
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
    services = compose.get("services")
    if not isinstance(services, dict) or not services or not isinstance(licenses, dict):
        raise ValueError("安装清单缺少服务或镜像许可")
    entry = config.get("entry")
    if entry and entry["service"] not in services:
        raise ValueError("安装清单入口不属于此应用")
    image_inputs = {}
    for name, service in services.items():
        if not identifier(name) or not isinstance(service, dict):
            raise ValueError("Compose 服务名称无效")
        allowed = {"build", "image", "command", "environment", "depends_on", "healthcheck", "volumes"}
        if set(service) - (allowed | ({"network_mode"} if builtin else set())):
            raise ValueError("请仅声明容器内的运行配置，安装器负责网络、端口和持久卷")
        for volume in service.get("volumes", []):
            parts = volume.split(":") if isinstance(volume, str) else []
            if (len(parts) not in (2, 3) or parts[0] not in (compose.get("volumes") or {})
                    or not parts[1].startswith("/") or (len(parts) == 3 and parts[2] not in {"ro", "rw"})):
                raise ValueError("安装包只接受应用命名数据卷")
        healthcheck = service.get("healthcheck")
        if (not isinstance(healthcheck, dict) or healthcheck.get("disable")
                or not isinstance(healthcheck.get("test"), list)
                or healthcheck["test"][:1] not in (["CMD"], ["CMD-SHELL"])):
            raise ValueError("每个独立服务都需要可执行的健康检查")
        environment = service.get("environment", {})
        if not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                        for k, v in environment.items()):
            raise ValueError("容器环境只能声明公开字符串，不能继承构建机或接收方的环境变量")
        if re.search(r"(?<!\$)\$(?!\$)", json.dumps(service, ensure_ascii=False)):
            raise ValueError("运行配置不能引用宿主环境，凭据应通过应用自己的首次设置保存")
        image_inputs[name] = _resolve_image_input(service, context, licenses.get(name))

    images = {}
    for name, image_input in image_inputs.items():
        if isinstance(image_input, _LocalImage):
            iid = stage / (name + ".iid")
            docker_runner("build", "--platform", "linux/amd64", "--iidfile", str(iid),
                          "--file", image_input.recipe, ".", cwd=image_input.directory)
            image_id = iid.read_text().strip()
        else:
            shutil.copyfile(image_input.license_file, payload / ("LICENSE-" + name + ".txt"))
            docker_runner("pull", "--platform", "linux/amd64", image_input.reference)
            image_id = docker_runner("image", "inspect", "--format", "{{.Id}}", image_input.reference)
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise ValueError("Docker 未返回镜像内容摘要")
        tag = f"agentstrata/{bot}-{name}:{version}-{image_id[7:19]}"
        docker_runner("tag", image_id, tag)
        images[tag] = image_id
        service = services[name].copy()
        service.pop("build", None)
        service.pop("image", None)
        service.update(image=tag, platform="linux/amd64", pull_policy="never", restart="unless-stopped")
        if entry and name == entry["service"]:
            service["ports"] = [f"127.0.0.1::{entry['port']}"]
        services[name] = service
    (payload / "compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False))
    docker_runner("image", "save", "-o", str(payload / "images.tar"), *images)
    (payload / "images.txt").write_text("".join(f"{tag} {value}\n" for tag, value in images.items()))
    if builtin:
        (payload / "receiver-setup").touch()
    if entry:
        (payload / "entry-port").write_text(f"{entry['service']} {entry['port']}\n")
    shutil.move(context, payload / "source")
    config["images"] = images
    return config
