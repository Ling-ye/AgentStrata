"""Owner tools and the workbench submit the same durable character jobs."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .character_types import character_id, profile, revision_number
from .preset_files import copy_file, digest, read_json
from .repository import private_directory, write_json
from .types import GenerationError, GenerationRequest, job_id
from .voice_assets import publish_voice
from .voice_sources import VoiceSource, materialize


def _sources(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise GenerationError("音源必须是列表")
    return [asdict(VoiceSource.parse(item)) for item in value]


def _submit(runtime, request, details, sources, *, request_id,
            character=None, base_version=None):
    frozen = {"profile": details, "sources": _sources(sources),
              "character_id": character, "base_version": base_version, "artwork": asdict(request)}
    fingerprint = hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    identifier = job_id(request_id) if request_id else fingerprint[:32]
    with runtime._lock:
        if runtime.repository.path(identifier).exists():
            original = read_json(runtime.repository.path(identifier) / "voice-request.json")
            if original.get("fingerprint") != fingerprint:
                raise GenerationError("同一请求编号已经对应另一项角色制作")
            return runtime.get(identifier)
        return runtime.start(request, None, character_id=character, details=details, identifier=identifier,
                             voice_request=frozen | {"fingerprint": fingerprint})


def generate(runtime, *, name: str, identity: str, response_style: str, artwork: str,
             sources: list[dict] | None = None, request_id: str | None = None) -> dict:
    if not isinstance(identity, str) or not identity.strip() or not isinstance(artwork, str) or not artwork.strip():
        raise GenerationError("角色制作需要身份描述和外观提示词")
    request = GenerationRequest.parse({"name": name, "prompt": artwork})
    details = profile({"name": request.name, "identity": identity, "response_style": response_style})
    return _submit(runtime, request, details, sources, request_id=request_id)


def train_voice(runtime, *, character: str, version: int, sources: list[dict] | None = None,
                request_id: str | None = None) -> dict:
    character, version = character_id(character), revision_number(version)
    details = runtime.characters.revision(character, version)["profile"]
    request = GenerationRequest.parse({"name": details["name"]})
    return _submit(runtime, request, details, sources, request_id=request_id,
                   character=character, base_version=version)


def select_model(repository, work: Path, request: dict) -> dict:
    source, _ = repository.revision(request["character_id"], request["base_version"])
    destination = private_directory(work / "model")
    copy_file(source / "avatar.zip", work / "model.zip")
    copy_file(source / "preview.png", destination / "preview.png")
    return {"sha256": digest(work / "model.zip"), "base_version": request["base_version"]}


def voice_configuration() -> Path:
    value = os.environ.get("AGENTSTRATA_GPT_SOVITS_CONFIG", "")
    path = Path(value)
    if not value or not path.is_absolute() or not path.is_file():
        raise GenerationError("请先通过桌宠安装器准备本机 GPT-SoVITS", code="voice_runtime_missing")
    return path


def voice_environment(*, inference: bool = False) -> dict[str, str]:
    names = ("PATH", "SystemRoot", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
             "ALLUSERSPROFILE", "LOCALAPPDATA", "APPDATA", "PYTHONPATH", "SYSTEMDRIVE")
    result = {name: os.environ[name] for name in names if name in os.environ}
    result.update(PYTHONUTF8="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    if inference:
        result["AGENTSTRATA_GPT_SOVITS_AUTH"] = os.environ["AGENTSTRATA_GPT_SOVITS_AUTH"]
    return result


def synthesize_job(repo, characters, identifier: str, request: dict) -> dict:
    root = repo.path(identifier)
    state = repo.read(identifier)
    try:
        # Keep the validated visual assets and source snapshot if training cannot start.
        state.update(status="running", stage="voice_sources")
        repo.save(identifier, state)
        work = private_directory(root / "voice-training")
        verified = work / "sources.json"
        resume = root / "voice-resume.json"
        original = repo.path(read_json(resume)["job"]) / "voice-training" if resume.is_file() else None
        if not verified.is_file():
            if original is not None and (original / "sources.json").is_file():
                records = read_json(original / "sources.json")["sources"]
                for record in records:
                    from .repository import relative_file
                    source = relative_file(original, record["file"])
                    if digest(source) != record["sha256"]:
                        raise GenerationError("原任务音源摘要不匹配，无法继续")
                    copy_file(source, relative_file(work, record["file"]))
            else:
                records = materialize(characters.repository.root, request["sources"], work)
            write_json(verified, {"sources": records})
        config = voice_configuration()
        settings = read_json(config)
        python = Path(settings["python"])
        command = [str(python), "-m", "agentstrata_live2d.voice_worker", "train", "--config", str(config),
                   "--work", str(work), "--job", str(root / "job.json")]
        # Training runs under the generator work process's owned Windows Job Object.
        if original is not None:
            command += ["--resume", str(original)]
        result = subprocess.run(command, cwd=work, env=voice_environment(), check=False)
        if result.returncode == 3:
            raise GenerationError("语音素材中可用的清晰单人片段不足", code="voice_input_required")
        if result.returncode == 4:
            raise GenerationError("GPT-SoVITS 训练显存不足", code="voice_out_of_memory")
        if result.returncode:
            raise GenerationError("GPT-SoVITS 训练或试听失败，阶段日志已保留", code="voice_training_failed")
        state = repo.read(identifier)
        with characters.repository.lock():
            voice = publish_voice(characters.repository.root, work / "voice")
        state.update(status="completed", stage="completed", voice_id=voice)
    except (OSError, ValueError, KeyError) as exc:
        state = repo.read(identifier)
        public = isinstance(exc, GenerationError)
        code = exc.code if public else "voice_training_failed"
        state.update(status="waiting_voice_input" if code == "voice_input_required" else "failed",
                     error=str(exc) if public else "语音阶段失败，请检查本机训练环境或任务日志", error_code=code)
    repo.save(identifier, state)
    return state
