"""Explicit QQ source preparation and accepted delivery, separate from configuration."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_directory, private_file
from chatcopilot.core.source_snapshot import source_manifest, manifest_digest
from chatcopilot.core.application_installation import application_source, installation
from chatcopilot.deployment.application_bundle import read_bundle
from chatcopilot.deployment.application_runtime import install_bundle


def _record(root):
    path = root / "source-binding.json"
    if not path.exists() and not path.is_symlink():
        return None
    private_file(path)
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("schema") != 1 or value.get("kind") not in {"prepared", "installed"}
            or any(not isinstance(value.get(key), str) for key in ("digest", "definition"))
            or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("digest", "")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("definition", "")))):
        raise ValueError("应用源码绑定无效")
    return value


def resolve(manager, project):
    root = manager._root(project)
    record = _record(root)
    if record is None:
        return None
    if record["definition"] != manager.definition_version(project):
        raise ValueError("应用配置已改变，请重新准备源码并独立验收")
    source = root / "sources" / record["digest"]
    if record["kind"] == "installed":
        source = root / "installations" / record["digest"]
        installed = installation(source)
        if installed is None or installed.bot_id != "qq-app-" + project:
            raise ValueError("应用安装身份不匹配")
        selected = application_source(source, installed.bot_id)
        if selected is None or selected[0] != source:
            raise ValueError("源码已由版本控制改变，请先重新绑定")
    if source.resolve() != source or manifest_digest(source_manifest(source)) != record["digest"]:
        raise ValueError("应用源码已改变，请重新准备并独立验收")
    return source, record


def status(manager, project):
    try:
        selected = resolve(manager, project)
    except (ValueError, OSError, RuntimeError):
        return {"state": "stale", "source_bound": False, "execution_verified": False,
                "definition_matches": False, "message": "源码或配置已改变，请重新准备并独立验收。"}
    if selected is None:
        return {"state": "unprepared", "source_bound": False, "execution_verified": False,
                "definition_matches": True, "message": "尚未准备独立源码；标准应用仍可配置和运行。"}
    _, record = selected
    return {"state": record["kind"], "source_bound": True, "source_digest": record["digest"],
            "execution_verified": False, "global_services_supported": False, "definition_matches": True,
            "message": "源码已绑定；仅应用专属开发服务可使用，真实模型和 QQ 收发仍须单独验证。"}


def prepare(manager, project):
    from studio.projects import project_inputs, source_tree
    from chatcopilot.core.application_configuration import load_application_spec
    root = manager._root(project)
    if manager._read(root) is None:
        raise ValueError("请先保存应用配置")
    previous = resolve(manager, project) if _record(root) and _record(root)["definition"] == manager.definition_version(project) else None
    if previous:
        return status(manager, project)
    manifest = project_inputs(manager.framework)
    # Public engine inputs only. Other apps, Studio state and provider values are excluded.
    manifest = {n: v for n, v in manifest.items() if n.startswith(("src/", "deploy/", "third_party/"))
                or n in {"pyproject.toml", "uv.lock", "requirements.txt", "LICENSE", "COPYRIGHT",
                         "TRADEMARKS.md", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.txt", "README.md"}}
    definition = manager.definition_version(project)
    token = hashlib.sha256((manifest_digest(manifest) + definition).encode()).hexdigest()
    parent = private_directory(root / "sources")
    def seed(target):
        app = target / "bots" / ("qq-app-" + project)
        app.mkdir(parents=True)
        for name in ("bot.yaml", "agent.yaml", "identity.md", "response.md"):
            private_file(root / name)
            shutil.copyfile(root / name, app / name)
        load_application_spec(app / "bot.yaml")
    source = source_tree(parent, "prepare-" + token, manager.framework, manifest, seed)
    digest = manifest_digest(source_manifest(source))
    destination = parent / digest
    if not destination.exists():
        source.rename(destination)
    if manifest_digest(source_manifest(destination)) != digest:
        raise ValueError("已有源码快照与准备结果不匹配")
    write_private(root / "source-binding.json", json.dumps(
        {"schema": 1, "kind": "prepared", "digest": digest, "definition": definition}))
    return status(manager, project)


def controller(manager, project, *, start_eval=False, task_id=None):
    from studio import runtime
    from chatcopilot.harness.store import HarnessStore
    from chatcopilot.harness.models import HarnessError
    root = manager._root(project)
    if task_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            raise ValueError("无效开发任务")
        matches = []
        for state in (root / "development").glob("*"):
            descriptor = state / "binding.json"
            if not descriptor.is_file() or not (state / "harness/harness.sqlite3").is_file():
                continue
            try:
                task = HarnessStore(state / "harness").get(task_id)
            except HarnessError as exc:
                if exc.code != "not_found":
                    raise
                continue
            if task.get("source", {}).get("bot_id") != "qq-app-" + project:
                raise ValueError("开发任务属于其他应用")
            private_file(descriptor)
            binding = json.loads(descriptor.read_text())
            source = root / ("installations" if binding["kind"] == "installed" else "sources") / state.name
            if source.resolve() != source or manifest_digest(source_manifest(source)) != state.name:
                raise ValueError("历史开发源码已改变")
            matches.append((source, state))
        if len(matches) != 1:
            raise ValueError("无法唯一定位此应用的开发任务")
        source, state = matches[0]
    else:
        selected = resolve(manager, project)
        if selected is None:
            raise ValueError("请先准备应用源码")
        source, record = selected
        state = private_directory(root / "development" / record["digest"])
        write_private(state / "binding.json", json.dumps(record))
    environment = dict(manager.model_environment)
    from chatcopilot.core.model_settings import ModelSettingsStore
    if manager.model_path.is_file():
        models = ModelSettingsStore(manager.model_path).read()
        for profile_id in set(models["bindings"].values()):
            profile = models["profiles"].get(profile_id, {})
            connection = models["connections"].get(profile.get("connection"), {})
            keys = (connection.get("auth", {}).get("key_env", ""),
                    connection.get("codex_bin_env", ""), connection.get("credential_root_env", ""))
            for key in keys:
                if key and key in os.environ:
                    environment[key] = os.environ[key]
    return runtime.controller(source, state, start_eval=start_eval, task_id=task_id,
                              application_checks=("decision-no-tool",), environment=environment)


def install(manager, project, task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
        raise ValueError("无效开发任务")
    root = manager._root(project)
    selected = resolve(manager, project)
    if selected is None:
        raise ValueError("请先准备应用源码")
    _, record = selected
    host = controller(manager, project, task_id=task_id)
    task = host.get(task_id)
    if record.get("installed_task") == task_id:
        return status(manager, project)
    if host.repository != selected[0]:
        raise ValueError("开发任务不属于当前应用源码基线")
    if task.get("source", {}).get("bot_id") != "qq-app-" + project:
        raise ValueError("开发任务属于其他应用")
    bundle = root / "deliveries" / record["digest"] / task_id
    # Revalidate the real independent acceptance, version facts and exact candidate.
    host.export_delivery(task_id, bundle)
    delivered = read_bundle(bundle)
    if delivered["bot_id"] != "qq-app-" + project:
        raise ValueError("交付物属于其他应用")
    relative = "bots/qq-app-" + project + "/bot.yaml"
    if delivered["bot_relative"] != relative:
        raise ValueError("交付物应用路径不匹配")
    # The first integration supports prompt improvement, not changes to permissions,
    # authentication, channel wiring or model settings.
    for name in ("bot.yaml", "agent.yaml"):
        if (bundle / "source" / Path(relative).parent / name).read_bytes() != (root / name).read_bytes():
            raise ValueError("交付物改变了受管配置；当前仅支持回复要求的源码改进")
    destination = root / "installations" / delivered["source_digest"]
    install_bundle(bundle, destination)
    if manifest_digest(source_manifest(destination)) != delivered["source_digest"]:
        raise ValueError("安装源码与交付物不一致")
    write_private(root / "source-binding.json", json.dumps(
        {"schema": 1, "kind": "installed", "digest": delivered["source_digest"],
         "definition": record["definition"], "installed_task": task_id}))
    return status(manager, project)
