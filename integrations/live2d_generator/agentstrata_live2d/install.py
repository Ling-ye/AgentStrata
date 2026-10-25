"""Explicit asset handoff to the VTuber client; never imports AgentRuntime."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
from uuid import uuid4

import yaml

from .repository import extract_zip, private_directory, relative_file, validate_bundle, write_bytes, write_json
from .types import GenerationError, job_id

CONFIG_FILES = ("data/live2d/model_dict.json", "config/adapter.json", "config/upstream.yaml")
CHARACTER_CONFIG_FILES = (*CONFIG_FILES, "config/identity.md", "config/response-style.md")


def _check_target(root: Path, config_files: tuple[str, ...] = CONFIG_FILES) -> dict:
    if root.resolve() != root or not root.is_dir():
        raise GenerationError("目标必须是现有插件安装的绝对路径，不得包含符号链接")
    record = json.loads(relative_file(root, "installation.json").read_text())
    if record.get("plugin_id") != "open-llm-vtuber" or record.get("root") != str(root):
        raise GenerationError("目标不是受管 Open-LLM-VTuber 安装")
    if not relative_file(root, "data/live2d/model_dict.json").is_file():
        raise GenerationError("客户端尚未支持持久模型目录，请先安装 open-llm-vtuber 0.3.0 或后续版本")
    if record.get("status") in {"installing", "failed", "uninstalled"}:
        raise GenerationError("客户端安装尚未就绪")
    services = record.get("services", [])
    if (not isinstance(services, list) or len(services) != 2
            or {s.get("name") for s in services if isinstance(s, dict)} != {"client", "gateway"}
            or any(type(s.get("port")) is not int or not 1 <= s["port"] <= 65535 for s in services)):
        raise GenerationError("客户端服务登记不完整")
    for relative in config_files:
        path = relative_file(root, relative)
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise GenerationError("客户端配置必须为当前用户的私有普通文件")
    return record


def require_stopped(root: Path, record: dict) -> None:
    for service in record.get("services", []):
        name = service["name"]
        if name not in {"client", "gateway"}:
            raise GenerationError("客户端服务清单无法识别")
        result = subprocess.run(["systemctl", "--user", "show", "--property=ActiveState", "--value",
                                 f"agentstrata-plugin-open-llm-vtuber-{name}.service"],
                                capture_output=True, text=True, timeout=10, check=False)
        if result.returncode or result.stdout.strip() not in {"inactive", "failed"}:
            raise GenerationError("导入前请先停止 open-llm-vtuber；无法确认 systemd 服务已停止")
        with socket.socket() as connection:
            connection.settimeout(1)
            if connection.connect_ex(("127.0.0.1", service["port"])) == 0:
                raise GenerationError("客户端端口仍在使用，拒绝修改运行中的模型")


@contextmanager
def target_lock(root: Path):
    if root.resolve() != root or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise GenerationError("目标安装目录必须属于当前用户且不得包含符号链接")
    fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        if os.fstat(lock.fileno()).st_nlink != 1:
            raise GenerationError("安装锁无效")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GenerationError("客户端已有安装或导入操作") from None
        yield


def install_bundle(bundle: Path, root: Path) -> dict:
    return _install(bundle, root)


def install_character(bundle: Path, details: dict, root: Path, *, identifier: str, version: int) -> dict:
    from .character_types import character_id, profile, revision_number
    identifier, version = character_id(identifier), revision_number(version)
    result = _install(bundle, root, details=profile(details))
    return result | {"character_id": identifier, "version": version}


def _install(bundle: Path, root: Path, *, details: dict | None = None) -> dict:
    root = root.expanduser().absolute()
    with target_lock(root):
        config_files = CHARACTER_CONFIG_FILES if details is not None else CONFIG_FILES
        record = _check_target(root, config_files)
        require_stopped(root, record)
        if details is not None:
            bot = yaml.safe_load(relative_file(root, "config/bot.yaml").read_text())
            prompts = bot.get("prompts", {}) if isinstance(bot, dict) else {}
            if (prompts.get("schema_version") != 2 or prompts.get("identity") != "identity.md"
                    or prompts.get("response_style") != "response-style.md"):
                raise GenerationError("客户端人设文件绑定已修改，请先恢复标准 identity/style 绑定")
            if not details["identity"].strip():
                raise GenerationError("应用角色前请填写身份描述并保存版本")
        journal = root / "config/pending-model-import.json"
        if journal.exists():
            raise GenerationError("存在中断的模型导入，请先执行 restore 恢复备份")
        with tempfile.TemporaryDirectory(prefix=".model-import-", dir=root / "data") as temporary:
            extracted = Path(temporary) / "asset"
            extract_zip(bundle, extracted, suffixes={".json", ".moc3", ".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3", ".txt", ".md"})
            metadata = validate_bundle(extracted)
            name = "generated-" + hashlib.sha256(bundle.read_bytes()).hexdigest()[:16]
            target = relative_file(root, "data/live2d/models/" + name)
            reused = target.exists()
            if reused:
                if details is None:
                    raise GenerationError("该模型已导入；如需恢复角色配置，请使用备份恢复命令")
                if validate_bundle(target)["sha256"] != metadata["sha256"]:
                    raise GenerationError("已安装模型内容与当前版本不一致")
            dictionary = json.loads((root / CONFIG_FILES[0]).read_text())
            if not isinstance(dictionary, list) or any(not isinstance(item, dict) for item in dictionary):
                raise GenerationError("客户端模型清单无效")
            existing = [item for item in dictionary if item.get("name") == name]
            if len(existing) > 1 or (existing and (not reused or existing[0].get("url") !=
                    f"/live2d-models/{name}/{metadata['model']}")):
                raise GenerationError("客户端模型清单名称重复或引用不一致")
            display_name = name
            manifest = extracted / "generation.json"
            if manifest.is_file():
                candidate = json.loads(manifest.read_text()).get("name")
                if isinstance(candidate, str) and 0 < len(candidate) <= 80:
                    display_name = candidate
            if details is not None:
                display_name = details["name"]
            expression_names = metadata["expressions"]
            # The upstream emotion mapper consumes expression indices.
            neutral = expression_names.get("neutral", 0)
            emotions = {key: expression_names.get(key, neutral) for key in
                        ("neutral", "joy", "sadness", "surprise", "anger", "fear", "disgust", "smirk")} if expression_names else {}
            if not existing:
                dictionary.append({"name": name, "description": display_name,
                    "url": f"/live2d-models/{name}/{metadata['model']}", "kScale": 0.5,
                    "initialXshift": 0, "initialYshift": 0, "kXOffset": 0,
                    "idleMotionGroupName": "Idle", "emotionMap": emotions, "tapMotions": {}})
            adapter = json.loads((root / CONFIG_FILES[1]).read_text())
            adapter.update(model_name=name, expressions=expression_names, motions=metadata["motions"])
            upstream = yaml.safe_load((root / CONFIG_FILES[2]).read_text())
            upstream["character_config"].update(live2d_model_name=name, character_name=display_name)
            if details is not None:
                tts = upstream["character_config"].setdefault("tts_config", {})
                tts["tts_model"] = "edge_tts"
                tts.setdefault("edge_tts", {})["voice"] = details["voice"]["voice"]
            values = [json.dumps(dictionary, ensure_ascii=False, indent=2).encode(),
                      json.dumps(adapter, ensure_ascii=False, indent=2).encode(),
                      yaml.safe_dump(upstream, allow_unicode=True, sort_keys=False).encode()]
            if details is not None:
                values.extend([details["identity"].encode(), details["response_style"].encode()])
            backup_id = uuid4().hex
            backup = private_directory(root / "data/live2d/backups" / backup_id)
            before = {}
            after = {}
            for relative, value in zip(config_files, values, strict=True):
                original = (root / relative).read_bytes()
                write_bytes(backup / relative, original)
                before[relative] = hashlib.sha256(original).hexdigest()
                after[relative] = hashlib.sha256(value).hexdigest()
            receipt = {"backup": backup_id, "root": str(root), "model": name, "before": before, "after": after}
            write_json(backup / "receipt.json", receipt)
            write_json(journal, {"backup": backup_id})
            try:
                private_directory(target.parent)
                if not reused:
                    extracted.rename(target)
                for relative, value in zip(config_files, values, strict=True):
                    write_bytes(root / relative, value)
                journal.unlink()
            except BaseException:
                # Keep the journal if recovery itself fails; the client refuses startup while pending.
                for relative in config_files:
                    write_bytes(root / relative, (backup / relative).read_bytes())
                journal.unlink()
                if not reused and target.exists():
                    shutil.rmtree(target)
                raise
            return {"model": name, "name": display_name, "backup": backup_id,
                    "next": "agentstrata plugin start open-llm-vtuber"}


def restore_bundle(root: Path, identifier: str) -> dict:
    root = root.expanduser().absolute()
    with target_lock(root):
        record = _check_target(root)
        require_stopped(root, record)
        backup = relative_file(root, "data/live2d/backups/" + job_id(identifier))
        receipt = json.loads((backup / "receipt.json").read_text())
        config_files = tuple(receipt.get("before", {}))
        if (receipt.get("root") != str(root) or set(config_files) not in (
                set(CONFIG_FILES), set(CHARACTER_CONFIG_FILES))
                or set(receipt.get("after", {})) != set(config_files)):
            raise GenerationError("备份身份不匹配")
        _check_target(root, config_files)
        originals = {}
        for relative in config_files:
            payload = relative_file(backup, relative).read_bytes()
            current = hashlib.sha256((root / relative).read_bytes()).hexdigest()
            if (hashlib.sha256(payload).hexdigest() != receipt["before"][relative]
                    or current not in {receipt["before"][relative], receipt["after"][relative]}):
                raise GenerationError("备份损坏或配置已有后续修改，拒绝覆盖")
            originals[relative] = payload
        journal = root / "config/pending-model-import.json"
        write_json(journal, {"backup": identifier})
        for relative, payload in originals.items():
            write_bytes(root / relative, payload)
        journal.unlink()
        return {"restored": identifier, "model_assets_retained": True}
