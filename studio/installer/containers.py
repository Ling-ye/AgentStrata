"""Freeze application-owned Docker contexts and save every image needed at install time."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import yaml

from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.core.source_snapshot import copy_sources
from studio.delivery import native_inputs
from studio.installer import BOTS, RECIPES, copy_public


def docker(*args, cwd=None):
    try:
        return subprocess.run(["docker", *args], cwd=cwd, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=1800).stdout.strip()
    except FileNotFoundError:
        raise ValueError("构建端需要可用的 Docker Engine") from None
    except subprocess.SubprocessError:
        raise ValueError(f"Docker {args[0]} 失败，请核对构建端的引擎、依赖来源及应用镜像配置") from None


def gateway_context(source, manifest, bot, kind, context):
    members = native_inputs(manifest, bot)
    members = {p: row for p, row in members.items() if not p.startswith("integrations/")}
    if f"bots/{bot}/bot.yaml" not in members:
        raise ValueError("所选版本缺少该机器人的 BotSpec")
    copy_sources(source, context, members)
    application = load_application_spec(context / "bots" / bot / "bot.yaml")
    profile = application.agent_profile.raw
    if kind in {"qq_personal", "weixin"} and profile.get("agents", {}).get("runtime") == "codex":
        # Package both declarations beside the frozen originals so relative resources and authority stay the same.
        alternate = application.source_path.with_name("receiver-langgraph.bot.yaml")
        agent = application.agent_profile.source_path.with_name("receiver-langgraph.agent.yaml")
        with agent.open("x", encoding="utf-8") as stream:
            yaml.safe_dump({**profile, "agents": {**profile["agents"], "runtime": "langgraph"}}, stream, allow_unicode=True)
        with alternate.open("x", encoding="utf-8") as stream:
            yaml.safe_dump({**application.raw, "agent_profile": agent.relative_to(alternate.parent).as_posix()}, stream, allow_unicode=True)
    shutil.copyfile(RECIPES / "gateway.py", context / "receiver.py")
    shutil.copyfile(RECIPES / "gateway.Dockerfile", context / "Dockerfile")
    service = {"build": ".", "volumes": ["data:/data"], "environment": {"BOT": bot},
               "healthcheck": {"test": ["CMD", "python", "/app/receiver.py", "health"], "interval": "5s",
                               "timeout": "4s", "retries": 18}}
    if kind == "qq_personal":
        # OneBot only accepts a loopback endpoint. Use the receiver's own provider namespace.
        service["network_mode"] = "container:napcat-" + bot
    return {"services": {"gateway": service}, "volumes": {"data": {}}}, {"setup": "gateway"}, {}


def project_context(source, manifest, bot, context, spec):
    copy_public(source, manifest, f"integrations/{bot}/", context)
    if not (context / "compose.yaml").is_file() or not (context / "install.json").is_file():
        raise ValueError("请在应用开发目录补齐 compose.yaml 和 install.json，再生成服务型安装包")
    declaration = yaml.safe_load((context / "compose.yaml").read_text())
    if not isinstance(declaration, dict) or set(declaration) - {"services", "volumes"}:
        raise ValueError("安装包 Compose 只接受本应用的 services 和命名 volumes")
    volumes = declaration.get("volumes") or {}
    if not isinstance(volumes, dict) or any(v not in (None, {}) or not identifier(k) for k, v in volumes.items()):
        raise ValueError("服务数据须使用本应用的命名卷")
    entry = spec.get("entry")
    if entry is not None and (not isinstance(entry, dict) or set(entry) != {"service", "port"}
                              or type(entry["port"]) is not int or not 1 <= entry["port"] <= 65535):
        raise ValueError("网页入口需指定 Compose 服务和容器端口")
    return declaration, {"entry": entry}, spec.get("image_licenses", {})


def identifier(name):
    return isinstance(name, str) and re.fullmatch("[a-z][a-z0-9-]*", name)


def prepare(source, manifest, bot, kind, stage, payload, version, spec):
    context = stage / "context"
    builtin = kind in BOTS
    compose, config, licenses = (gateway_context(source, manifest, bot, kind, context) if builtin else
                                 project_context(source, manifest, bot, context, spec))
    # The outer staging directory is private. Docker COPY must preserve readable public
    # source files even when the runtime image uses an unprivileged application user.
    for path in [context, *context.rglob("*")]:
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
    services = compose.get("services")
    if not isinstance(services, dict) or not services or not isinstance(licenses, dict):
        raise ValueError("安装清单缺少服务或镜像许可")
    entry = config.get("entry")
    if entry and entry["service"] not in services:
        raise ValueError("安装清单入口不属于此应用")
    images = {}
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
        if not isinstance(service.get("healthcheck"), dict) or service["healthcheck"].get("disable") or (
            not isinstance(service["healthcheck"].get("test"), list)
            or service["healthcheck"]["test"][:1] not in (["CMD"], ["CMD-SHELL"])
        ):
            raise ValueError("每个独立服务都需要可执行的健康检查")
        environment = service.get("environment", {})
        if not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
            raise ValueError("容器环境只能声明公开字符串，不能继承构建机或接收方的环境变量")
        # Compose must never substitute a recipient's unrelated host environment.
        if re.search(r"(?<!\$)\$(?!\$)", json.dumps(service, ensure_ascii=False)):
            raise ValueError("运行配置不能引用宿主环境，凭据应通过应用自己的首次设置保存")
        build = service.pop("build", None)
        image = service.pop("image", None)
        tag = f"agentstrata/{bot}-{name}:{version}"
        if build is not None and image is None:
            recipe = "Dockerfile"
            if isinstance(build, dict):
                if set(build) - {"context", "dockerfile"}:
                    raise ValueError("应用服务构建只接受本地 context 和 dockerfile")
                recipe = build.get("dockerfile", recipe)
                build = build.get("context", ".")
            if any(not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts
                   for value in (build, recipe)):
                raise ValueError("Docker 构建目录和配方须位于本应用公开源码内")
            directory = context / build
            dockerfile = directory / recipe
            if not dockerfile.is_file() or not dockerfile.resolve().is_relative_to(context.resolve()):
                raise ValueError("应用服务缺少本地 Dockerfile")
            # An immutable image ID avoids exporting another concurrent build's tag.
            iid = stage / (name + ".iid")
            docker("build", "--platform", "linux/amd64", "--iidfile", str(iid), "--file", recipe, ".", cwd=directory)
            image_id = iid.read_text().strip()
        elif isinstance(image, str) and build is None and re.fullmatch(r"[\w./:-]+@sha256:[0-9a-f]{64}", image):
            notice = licenses.get(name)
            if not isinstance(notice, str) or Path(notice).is_absolute() or ".." in Path(notice).parts or not (context / notice).is_file():
                raise ValueError("随包第三方镜像须固定摘要并提供再分发许可说明")
            shutil.copyfile(context / notice, payload / ("LICENSE-" + name + ".txt"))
            docker("pull", "--platform", "linux/amd64", image)
            image_id = docker("image", "inspect", "--format", "{{.Id}}", image)
        else:
            raise ValueError("每个服务必须指定一个构建目录或固定摘要的可分发镜像")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise ValueError("Docker 未返回镜像内容摘要")
        tag += "-" + image_id[7:19]
        docker("tag", image_id, tag)
        images[tag] = image_id
        service.update(image=tag, platform="linux/amd64", pull_policy="never", restart="unless-stopped")
        if entry and name == entry["service"]:
            service["ports"] = [f"127.0.0.1::{entry['port']}"]
    (payload / "compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False))
    docker("image", "save", "-o", str(payload / "images.tar"), *images)
    (payload / "images.txt").write_text("".join(f"{tag} {value}\n" for tag, value in images.items()))
    if builtin:
        (payload / "receiver-setup").touch()
    if entry:
        (payload / "entry-port").write_text(f"{entry['service']} {entry['port']}\n")
    # Keep scripts and corresponding application sources accessible to the recipient.
    shutil.move(context, payload / "source")
    config["images"] = images
    return config
