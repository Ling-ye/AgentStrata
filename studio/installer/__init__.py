"""One downloadable setup program per application version and operating system."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from chatcopilot.core.source_manifest import is_deployable_source_path
from chatcopilot.core.source_snapshot import copy_sources
from studio import delivery

RECIPES = Path(__file__).parent
WINDOWS = "windows-x64"
LINUX = "linux-x64"
FILES = {WINDOWS: "application.exe", LINUX: "application.run"}
WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{name}{digit}" for name in ("COM", "LPT") for digit in "123456789¹²³"}
BOTS = {"qq_personal", "qq_official", "weixin"}


@lru_cache(maxsize=1)
def recipe_digest():
    """A new packaging recipe can regenerate the same application revision after a Studio update."""
    digest = hashlib.sha256()
    root = RECIPES.parents[1]
    sources = [*RECIPES.iterdir(), *(root / "integrations/desktop_app").iterdir(),
               root / "studio/delivery.py", root / "integrations/live2d_desktop/scripts/prepare-runtime.py"]
    for path in sorted(p for p in sources if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def platforms(kind):
    return [WINDOWS] if kind in {"desktop", "companion"} else [WINDOWS, LINUX]


def requirements(kind, bot):
    sandbox = "首次配置可明确选择 Codex 或 LangGraph。Codex 需要 Docker 支持普通用户的 bubblewrap 会话隔离；LangGraph 需要兼容的聊天模型连接。"
    if kind == "qq_personal":
        return [f"自备已登录的 NapCat（OneBot v11 正向 WebSocket），容器名 napcat-{bot}，在其回环地址启用服务。",
                "首次安装时绑定自己的 QQ、Owner 账号和模型账号。", sandbox]
    if kind == "weixin":
        return ["首次安装时绑定自己的微信 iLink 账号和模型账号。", sandbox]
    if kind == "qq_official":
        return ["首次安装时填写自己的 QQ AppID、AppSecret 和模型账号。"]
    return []


def layout(kind, bot, spec, compose):
    """Development targets seed a default; the frozen app declares its actual startup and services."""
    if not isinstance(spec, dict) or set(spec) - {"entry", "clients", "requirements", "image_licenses"}:
        raise ValueError("install.json 含有未支持的字段")
    clients = spec.get("clients", {})
    if not isinstance(clients, dict) or set(clients) - FILES.keys():
        raise ValueError("客户端入口请按支持的操作系统声明")
    for target, value in clients.items():
        if (not isinstance(value, str) or not value.isprintable() or "\\" in value
                or PurePosixPath(value).as_posix() != value or PurePosixPath(value).is_absolute()
                or not PurePosixPath(value).parts or ".." in PurePosixPath(value).parts
                or target == WINDOWS and Path(value).suffix.lower() not in {".exe", ".html", ".htm"}):
            raise ValueError("客户端必须指向本应用公开目录中的可启动文件")
    needs = spec.get("requirements", [])
    if not isinstance(needs, list) or not all(isinstance(n, str) and n.strip() for n in needs):
        raise ValueError("请逐项说明接收方需要准备的组件或账号")
    if kind in {"desktop", "companion"} and not compose and not clients:
        needs = ["首次打开时按提示准备本地运行环境，可能需要联网下载依赖，并绑定自己的模型账号。", *needs]
    return {"platforms": [p for p in FILES if p in clients] if clients else platforms(kind),
            "docker": kind in BOTS or compose or kind == "source" and not clients,
            "clients": clients, "requirements": requirements(kind, bot) + needs}


def copy_public(source, manifest, prefix, dest):
    entries = {}
    names = set()
    for name, row in manifest.items():
        if not name.startswith(prefix):
            continue
        relative = name[len(prefix):]
        parts = relative.split("/")
        if (not is_deployable_source_path(name) or any(
                not p or p in {".", ".."} or p.endswith((" ", ".")) or p.partition(".")[0].upper() in WINDOWS_DEVICES
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', p) for p in parts)
                or relative.casefold() in names):
            raise ValueError("应用资源中有无法安全安装的跨平台文件名")
        names.add(relative.casefold())
        entries[relative] = row
    copy_sources(source / prefix.rstrip("/"), dest, entries)


def windows_installer(stage, payload_hash, bot, docker):
    script = (RECIPES / "windows.nsi").read_text().replace("@SHA@", payload_hash).replace("@BOT@", bot)
    script = script.replace("@DOCKER@", "-DockerRequired" if docker else "")
    (stage / "setup.nsi").write_text(script, encoding="utf-8-sig")
    if shutil.which("makensis"):
        command = ["makensis", "-V2", "setup.nsi"]
    elif shutil.which("node") and (RECIPES.parents[1] / "integrations/live2d_desktop/node_modules/app-builder-lib").is_dir():
        command = ["node", str(RECIPES.parents[1] / "integrations/desktop_app/nsis.cjs")]
    else:
        raise ValueError("Windows 安装包构建端需要 NSIS 3，或 integrations/live2d_desktop 的锁定 Node 构建工具")
    subprocess.run(command, cwd=stage, check=True, timeout=900)


def build(source, dest, *, bot, kind, name, manifest, origin, targets, release=None, assets=None):
    """source is an immutable Git checkout or a checked, independently accepted candidate."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", bot):
        raise ValueError("应用标识或安装平台无效")
    if not name.strip() or not name.isprintable():
        raise ValueError("应用名称无效")
    base = origin["revision"]
    prefix = f"integrations/{bot}/"
    descriptor = prefix + "install.json"
    spec = {}
    if descriptor in manifest:
        raw = (source / descriptor).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest[descriptor]["sha256"]:
            raise ValueError("安装清单与已选源码的摘要不符")
        spec = json.loads(raw)
    plan = layout(kind, bot, spec, prefix + "compose.yaml" in manifest)
    targets = plan["platforms"] if targets is None else targets
    if not targets or not set(targets) <= set(plan["platforms"]):
        raise ValueError("所选版本不支持这个安装平台")
    needs, needs_docker, clients = plan["requirements"], plan["docker"], plan["clients"]
    native = kind == "companion" or kind == "desktop" and not clients and not needs_docker
    if native:
        install_id = bot + "-" + base[:10]
        with tempfile.TemporaryDirectory(prefix="studio-native-assets-") as tmp:
            if kind == "companion" and assets is None:
                from chatcopilot.deployment.windows_dependencies import compiler_path
                root = RECIPES.parents[1]
                assets = Path(tmp) / "payload"
                preparation = subprocess.run([sys.executable, str(root / "integrations/live2d_desktop/scripts/prepare-runtime.py"),
                                "--source", str(source), "--output", str(assets), "--build-root", str(Path(tmp) / "work"),
                                "--uv", str(compiler_path())], stderr=subprocess.PIPE, text=True, timeout=1800)
                if preparation.returncode:
                    if "runtime-inputs" in preparation.stderr or "MCP source policy" in preparation.stderr:
                        raise ValueError("构建端缺少桌宠锁定的 Windows-MCP 原始输入。请准备源码、Python 归档及完整 wheel 缓存，或导入与所选版本匹配的 Windows 运行载荷。")
                    if "patched project sources are incomplete" in preparation.stderr:
                        raise ValueError("桌宠缺少当前锁文件对应的插件 wheel、配套源码或上游项目归档。")
                    if "lack runtime admission" in preparation.stderr:
                        raise ValueError("桌宠插件的原始输入已核对，Windows 前端资源、运行环境与安装身份仍需独立验收。请导入已核验的匹配载荷。")
                    raise ValueError("桌宠 Windows 资源准备失败。请核对原生构建输入，或在「接入已有应用」导入匹配的已核验载荷。")
            receipt = delivery.pack(source, dest, bot_relative=f"bots/{bot}/bot.yaml", release=release,
                                    assets=assets, name=name, install_id=install_id,
                                    windows_build=os.environ.get("AGENTSTRATA_STUDIO_WINDOWS_BUILD"))
        components = receipt["components"]
    else:
        from studio.installer import containers
        dest.mkdir(parents=True, exist_ok=False)
        receipt = {}
        with tempfile.TemporaryDirectory(dir=dest) as tmp:
            stage = Path(tmp)
            payload = stage / "payload"
            payload.mkdir(mode=0o700)
            if needs_docker:
                config = containers.prepare(source, manifest, bot, kind, stage, payload, base, spec)
            elif clients:
                copy_public(source, manifest, prefix, payload / "source")
                config = {}
            elif kind == "web":
                copy_public(source, manifest, f"integrations/{bot}/", payload / "site")
                if not (payload / "site/index.html").is_file():
                    raise ValueError("已选源码缺少应用入口 index.html")
                config = {"entry": "site/index.html"}
            else:
                raise ValueError("此应用缺少安装入口")
            if clients:
                installed = {}
                for platform in targets:
                    target = "source/" + clients[platform]
                    path = payload / target
                    if not path.is_file() or (platform == LINUX and path.suffix.lower() not in {".htm", ".html"}
                                              and not path.stat().st_mode & 0o111):
                        raise ValueError("所选源码中的客户端入口缺失或不可执行")
                    installed[platform] = target
                config["clients"] = installed
                if LINUX in installed:
                    (payload / "client-linux").write_text(installed[LINUX] + "\n")
            if needs_docker and config.get("entry") and WINDOWS in targets:
                (payload / "open-client.ps1").write_text((RECIPES / "open-client.ps1").read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
            for legal in ("LICENSE", "COPYRIGHT", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.txt"):
                if legal in manifest:
                    copy_sources(source, payload, {legal: manifest[legal]})
            components = [*config.get("images", {})]
            if needs_docker:
                components.append("Docker Compose 部署配置")
            if clients:
                components.extend(f"{platform} 客户端 · {entry}" for platform, entry in installed.items())
            elif kind == "web" and not needs_docker:
                components.append("网页及静态资源 · site/index.html")
            config.update(application=bot, name=name, origin=origin, docker=needs_docker, requirements=needs)
            (payload / "install.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            archive = stage / "payload.tgz"
            with tarfile.open(archive, "w:gz", compresslevel=1) as tar:
                for file in sorted(payload.iterdir()):
                    tar.add(file, arcname=file.name)
            checksum = delivery.digest(archive)
            if WINDOWS in targets:
                for script in (RECIPES / "install.ps1", RECIPES.parents[1] / "integrations/desktop_app/install-receipt.ps1"):
                    (stage / script.name).write_text(script.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
                windows_installer(stage, checksum, bot, needs_docker)
                (stage / FILES[WINDOWS]).rename(dest / FILES[WINDOWS])
            if LINUX in targets:
                header = (RECIPES / "install.sh").read_text().replace("@SHA@", checksum).replace("@BOT@", bot)
                header = header.replace("@DOCKER@", "1" if needs_docker else "0")
                with (dest / FILES[LINUX]).open("xb") as output, archive.open("rb") as data:
                    output.write(header.encode())
                    shutil.copyfileobj(data, output)
                (dest / FILES[LINUX]).chmod(0o700)
    receipt.update(application=bot, origin=origin, docker=needs_docker, requirements=needs, components=components,
                   installers={p: {"sha256": delivery.digest(dest / FILES[p]), "size": (dest / FILES[p]).stat().st_size}
                               for p in targets})
    if release:
        receipt["accepted_source_digest"] = release.receipt["candidate_digest"]
    (dest / "build-receipt.json").write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    return receipt
