"""Create single-application sources and import a selected native desktop's public baseline."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import yaml

from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.source_manifest import is_deployable_source_path
from chatcopilot.core.source_snapshot import copy_sources, git_output, manifest_digest, source_manifest
from chatcopilot.deployment.application_runtime import _initialize_source


def source_tree(parent, name, source, manifest, prepare=None):
    """Publish a complete private Git baseline only after all the bytes have been verified."""
    destination = parent / name
    if destination.is_dir():
        return destination
    pending = Path(tempfile.mkdtemp(prefix=".source-", dir=private_directory(parent)))
    try:
        copy_sources(source, pending, manifest)
        if prepare:
            prepare(pending)
        from chatcopilot.core.source_manifest import filter_existing_source_paths
        files = filter_existing_source_paths(pending, (p.relative_to(pending).as_posix()
                                              for p in pending.rglob("*") if p.is_file()))
        contents = {n: {"sha256": trusted_source_sha256(pending / n, root=pending,
                         max_bytes=max((pending / n).stat().st_size, 1)), "executable": bool((pending / n).stat().st_mode & 0o111)}
                    for n in files}
        _initialize_source(pending, contents)
        pending.rename(destination)
    finally:
        if pending.exists():
            shutil.rmtree(pending)
    return destination


def project_inputs(framework):
    """Select the platform's public inputs before projecting a single application."""
    tracked = set(git_output(framework, "ls-files").splitlines())
    official = source_manifest(framework)
    source_areas = ("studio/", "integrations/desktop_app/", "integrations/desktop-reader/",
                    "bots/desktop-reader/", "bots/studio-guide/")
    new_files = {"src/chatcopilot/windows_setup.py", "specs/desktop-app-studio/spec.md",
                 "docs/guides/desktop-studio.md", "tests/unit/test_desktop_studio.py"}
    return {p:v for p,v in official.items() if p in tracked or p in new_files or p.startswith(source_areas)}


def new_project(framework, parent, bot_id, name):
    from studio.delivery import native_inputs

    manifest = native_inputs(project_inputs(framework), bot_id)
    def seed(root):
        app = root / "bots" / bot_id
        template = framework / "bots/desktop-reader"
        for relative in ("bot.yaml", "agent.yaml", "mcp/servers.yaml", "prompts/identity.md", "prompts/response-style.md"):
            (app / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template / relative, app / relative)
        bot = yaml.safe_load((app / "bot.yaml").read_text())
        bot.update(id=bot_id, display_name=name)
        bot["deploy"]["instance_id"] = bot_id
        (app / "bot.yaml").write_text(yaml.safe_dump(bot, allow_unicode=True, sort_keys=False))
        mcp = app / "mcp/servers.yaml"
        mcp.write_text(mcp.read_text().replace('"desktop-reader"', json.dumps(bot_id)))
        (app / "prompts/identity.md").write_text(f"你是{name}的应用助手。用当前应用实际提供的操作帮助用户。\n")
        shutil.copytree(framework / "studio/seed", root / "integrations" / bot_id)
        tests = root / "tests/unit"
        tests.mkdir(parents=True)
        contract = (framework / "studio/seed_tests.py").read_text()
        (tests / "test_desktop_application.py").write_text(contract.replace("__APPLICATION_ID__", bot_id))
        load_application_spec(app / "bot.yaml")
    return source_tree(parent, "initial", framework, manifest, seed)


def linux_path(windows):
    return Path(subprocess.check_output(["wslpath", "-u", windows], text=True).strip())


def select_live2d():
    script = Path(__file__).resolve().parents[1] / "integrations/desktop_app/select-live2d.ps1"
    win = subprocess.check_output(["wslpath", "-w", str(script)], text=True).strip()
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                                 "-File", win], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=300, check=True)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("等待选择桌宠超时。请再次点击「连接 Live2D 桌宠」并在 Windows 窗口选择程序。") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError("无法连接桌宠。请先打开已安装的 AgentStrata Live2D Native 完成本机准备，再重试。") from exc
    selection = json.loads(result.stdout.lstrip("\ufeff"))
    if not selection:
        raise ValueError("未选择桌宠")
    return linux_path(selection["root"]), linux_path(selection["assets"])


def import_live2d(parent, root, assets):
    from chatcopilot.deployment.application_bundle import read_bundle
    package = read_bundle(assets, native_volume=True)
    installed = json.loads((root / ".agentstrata-application.json").read_text())
    manifest = json.loads((root / ".agentstrata-source.json").read_text())
    if ((root / ".agentstrata-versions/current.json").exists()
            or package["bot_id"] != "live2d-desktop" or installed.get("bot_id") != package["bot_id"]
            or installed.get("platform") != "windows-x86_64"
            or installed.get("artifacts_digest") != package.get("artifacts_digest")
            or manifest != package["source_manifest"] or installed.get("delivered_digest") != manifest_digest(manifest)):
        raise ValueError("所选桌宠的实际安装版本与运行载荷不一致")
    if any(not is_deployable_source_path(p) for p in manifest):
        raise ValueError("Invalid public source manifest")
    return source_tree(parent, "initial", root, manifest)


def socket_path(root, verification=""):
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    key = hashlib.sha256((str(root) + ("\0" + verification if verification else "")).encode()).hexdigest()[:20]
    return private_directory(base / "as-studio") / (key + ".sock")
