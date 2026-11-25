"""Same-origin character HTTP input/output adapters."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import tempfile

from fastapi import Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .runtime import configured_character_target
from .types import GenerationError, MAX_UPLOAD


async def body(request: Request, allowed: set[str], *, upload: bool = False) -> dict:
    limit = MAX_UPLOAD * 4 // 3 + 512 * 1024 if upload else 512 * 1024
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise GenerationError("请求体积超限")
    try:
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise GenerationError("请求 JSON 或字段无效") from None


def upload_file(value: dict, destination: Path) -> Path:
    try:
        if not isinstance(value, dict) or set(value) != {"filename", "data"}:
            raise ValueError
        content = base64.b64decode(value["data"], validate=True)
        if not content or len(content) > MAX_UPLOAD:
            raise ValueError
        destination.write_bytes(content)
        destination.chmod(0o600)
        return destination
    except (ValueError, TypeError, KeyError):
        raise GenerationError("上传文件无效或超过 48 MiB") from None


def register(app, runtime):
    characters, presets = runtime.characters, runtime.presets

    @app.post("/api/voice-inputs")
    async def voice_input(request: Request):
        from .voice_sources import upload_audio
        value = await body(request, {"input", "source", "permission"}, upload=True)
        media = value.get("input")
        if not isinstance(media, dict) or not isinstance(media.get("filename"), str):
            raise GenerationError("请选择音源文件")
        suffix = Path(media["filename"]).suffix.lower()
        with tempfile.TemporaryDirectory(prefix=".voice-upload-", dir=runtime.repository.root) as temporary:
            source = upload_file(media, Path(temporary) / ("audio" + suffix))
            return await run_in_threadpool(upload_audio, characters.repository.root, source, value.get("source"),
                                           value.get("permission"))

    @app.post("/api/character-jobs")
    async def generate(request: Request):
        from .voice_jobs import generate as submit
        value = await body(request, {"name", "identity", "response_style", "artwork", "sources", "request_id"})
        try:
            return await run_in_threadpool(submit, runtime, **value)
        except TypeError:
            raise GenerationError("角色制作输入不完整") from None

    @app.post("/api/characters/{identifier}/versions/{version}/train-voice")
    async def train_voice(identifier: str, version: int, request: Request):
        from .voice_jobs import train_voice as submit
        value = await body(request, {"sources", "request_id"})
        return await run_in_threadpool(submit, runtime, character=identifier, version=version, **value)

    @app.get("/api/characters/{identifier}/versions/{version}/audio/{sample}")
    def voice_preview(identifier: str, version: int, sample: str):
        return FileResponse(characters.voice_preview(identifier, version, sample), media_type="audio/wav")

    @app.get("/api/characters")
    def listing():
        return {"characters": characters.list()}

    @app.post("/api/characters")
    async def create(request: Request):
        value = await body(request, {"profile"})
        return await run_in_threadpool(characters.create, value.get("profile"))

    @app.post("/api/characters/collect")
    async def collect(request: Request):
        value = await body(request, {"job", "id"})
        return await run_in_threadpool(characters.collect_job, value.get("job", ""), value.get("id"))

    @app.post("/api/characters/import-model")
    async def import_model(request: Request):
        value = await body(request, {"input", "profile", "id", "preview"}, upload=True)
        if bool(value.get("id")) == bool(value.get("profile")):
            raise GenerationError("导入模型请选择已有角色或提供新角色资料")
        with tempfile.TemporaryDirectory(prefix="as-character-upload-") as temporary:
            root = Path(temporary)
            model = upload_file(value.get("input"), root / "model.zip")
            preview = upload_file(value["preview"], root / "preview.png") if "preview" in value else None
            return await run_in_threadpool(characters.import_model, model, value.get("profile"),
                                           identifier=value.get("id"), preview=preview)

    @app.post("/api/characters/import-preset")
    async def import_preset(request: Request):
        value = await body(request, {"input"}, upload=True)
        with tempfile.TemporaryDirectory(prefix="as-preset-upload-") as temporary:
            source = upload_file(value.get("input"), Path(temporary) / "preset.zip")
            return await run_in_threadpool(characters.import_preset, source)

    @app.post("/api/characters/restore-application")
    async def restore_application(request: Request):
        value = await body(request, {"backup"})
        return await run_in_threadpool(characters.restore_application, configured_character_target(),
                                      value.get("backup", ""))

    @app.get("/api/characters/{identifier}")
    def character(identifier: str):
        return characters.get(identifier)

    @app.post("/api/characters/{identifier}/draft")
    async def draft(identifier: str, request: Request):
        value = await body(request, {"profile"})
        return await run_in_threadpool(characters.save_draft, identifier, value.get("profile"))

    @app.post("/api/characters/{identifier}/versions")
    async def save_version(identifier: str, request: Request):
        value = await body(request, {"base_version"})
        return await run_in_threadpool(characters.save_version, identifier, value.get("base_version"))

    @app.get("/api/characters/{identifier}/versions/{version}")
    def version_info(identifier: str, version: int):
        return characters.revision(identifier, version)

    @app.get("/api/characters/{identifier}/versions/{version}/preview.png")
    def preview(identifier: str, version: int):
        return FileResponse(characters.preview(identifier, version), media_type="image/png")

    @app.post("/api/characters/{identifier}/versions/{version}/export")
    def export(identifier: str, version: int):
        root = Path(tempfile.mkdtemp(prefix="as-preset-export-"))
        try:
            target = root / "preset.zip"
            characters.export(identifier, version, target)
            return FileResponse(target, media_type="application/zip",
                filename=f"character-{identifier[:8]}-v{version}.zip",
                background=BackgroundTask(shutil.rmtree, root))
        except BaseException:
            shutil.rmtree(root)
            raise

    @app.post("/api/characters/{identifier}/versions/{version}/fork")
    def fork(identifier: str, version: int):
        return characters.fork(identifier, version)

    @app.post("/api/characters/{identifier}/versions/{version}/apply")
    def apply(identifier: str, version: int):
        return characters.apply(identifier, version, configured_character_target())

    @app.get("/api/presets")
    def catalog():
        return presets.list()

    @app.post("/api/presets/refresh")
    def refresh():
        return presets.refresh()

    @app.post("/api/presets/{identifier}/{version}/download")
    def download(identifier: str, version: int):
        return presets.download(identifier, version)

    @app.get("/api/presets/{identifier}/{version}/preview.png")
    def preset_preview(identifier: str, version: int):
        return FileResponse(presets.preview(identifier, version), media_type="image/png")
