"""Asset production use case, independent of HTTP, processes and Agent runtimes."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import json
import time
from typing import Callable

from .repository import JobRepository, image_info, make_archive, validate_bundle, write_json
from .types import (Cancelled, GenerationError, GenerationRequest, ImageProvider, INPUT_SUFFIXES,
                    LayerProvider, RigProvider)


class GenerationService:
    def __init__(self, repository: JobRepository, image: ImageProvider, layers: LayerProvider,
                 rig: RigProvider, timeout: int = 1800):
        self.repository, self.image, self.layers, self.rig = repository, image, layers, rig
        self.timeout = timeout

    def submit(self, request: GenerationRequest, source: Path | None) -> str:
        if source is None and not (request.prompt or request.information):
            raise GenerationError("请提供提示词、角色资料或图片/分层素材")
        if source is not None and source.suffix.lower() not in INPUT_SUFFIXES:
            raise GenerationError("支持 PNG、JPEG、WebP、PSD 或 PNG 图层 ZIP")
        if source is not None and source.suffix.lower() in {".psd", ".zip"} and request.redraw:
            raise GenerationError("分层素材不能使用重新绘制")
        return self.repository.create(asdict(request), source)

    def run(self, identifier: str, cancelled: Callable[[], bool] = lambda: False) -> dict:
        repo, root = self.repository, self.repository.path(identifier)
        state = repo.read(identifier)
        if state["status"] != "queued":
            raise GenerationError("仅可执行尚未开始的任务")
        request = GenerationRequest.parse(json.loads((root / "request.json").read_text()))
        started = time.monotonic()

        def check():
            if cancelled():
                raise Cancelled("生成已取消；远端已提交的推理可能继续运行")
            if time.monotonic() - started > self.timeout:
                raise GenerationError("生成超过配置的时间限制；远端请求不会自动重试")

        def stage(value: str):
            check()
            state.update(status="running", stage=value)
            repo.save(identifier, state)

        try:
            inputs = list(root.glob("input.*"))
            source = inputs[0] if inputs else None
            if source is None or request.redraw:
                stage("illustration")
                self.image.generate(request.image_prompt(), source, root / "illustration.png")
                source = root / "illustration.png"
            if source.suffix.lower() not in {".psd", ".zip"}:
                stage("decomposition")
                image_info(source)
                self.layers.decompose(source, root / "layers.psd", check)
                source = root / "layers.psd"
            stage("rigging")
            result = self.rig.build(source, root / "model", root / "layers")
            stage("validation")
            validation = validate_bundle(root / "model")
            manifest = {"format": "agentstrata-live2d-v1", "name": request.name,
                        "model": validation["model"], "generation": result,
                        "actions": {k: validation[k] for k in ("expressions", "motions")},
                        "files": validation["sha256"],
                        "validation": validation["validation"]}
            write_json(root / "model/generation.json", manifest)
            stage("packaging")
            make_archive(root / "model", root / "model.zip")
            check()
            state.update(status="completed", stage="completed", result=manifest)
        except Cancelled as exc:
            state.update(status="cancelled", error=str(exc))
        except GenerationError as exc:
            state.update(status="failed", error=str(exc))
            if exc.code:
                state["error_code"] = exc.code
            if exc.retry_after_seconds is not None:
                state["retry_at"] = datetime.fromtimestamp(
                    time.time() + exc.retry_after_seconds, timezone.utc).isoformat()
        except Exception as exc:
            # Provider/library exceptions may contain credentials, URLs or source image contents.
            state.update(status="failed", error=f"{state['stage']} 阶段失败（{type(exc).__name__}）")
        repo.save(identifier, state)
        return state
