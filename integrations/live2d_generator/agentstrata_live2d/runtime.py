"""Assembly and one bounded child process. No model is loaded in the host process."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .config import Config, load_environment
from .repository import JobRepository, image_info, private_directory, write_json
from .preset_files import copy_file, digest, read_json
from .service import GenerationService
from .types import GenerationError, GenerationRequest, TERMINAL
from . import voice_jobs


def service(root: Path, config: Config) -> GenerationService:
    from .providers import ImagesAPI, SeeThroughHTTP, SeeThroughSpace
    from .rigging import AutomaticRigger

    return GenerationService(JobRepository(root), ImagesAPI(config),
                             SeeThroughSpace(config) if config.decompose_space else SeeThroughHTTP(config),
                             AutomaticRigger(), config.timeout_seconds)


def worker(root: Path, identifier: str) -> int:
    repository = JobRepository(root)
    config = Config.load(repository.path(identifier) / "config.json")
    work = repository.path(identifier)
    voice_input = work / "voice-request.json"
    request = read_json(voice_input) if voice_input.is_file() else None
    result = repository.read(identifier)
    if result.get("model_sha256"):
        if digest(work / "model.zip") != result["model_sha256"]:
            raise GenerationError("已保存模型的摘要不匹配")
        result.update(status="completed", stage="completed")
        repository.save(identifier, result)
    elif request is not None and request.get("base_version") is not None:
        characters, _ = character_services(root)
        model = voice_jobs.select_model(characters.repository, work, request)
        result.update(status="completed", stage="completed", result=model, model_sha256=model["sha256"])
        repository.save(identifier, result)
    else:
        result = service(root, config).run(identifier)
    if result["status"] == "completed" and request is not None:
        characters, _ = character_services(root)
        result = voice_jobs.synthesize_job(repository, characters, identifier, request)
    if result["status"] == "completed" and result.get("character_id"):
        try:
            characters, _ = character_services(root)
            saved = characters.collect_job(identifier)
            result["character_version"] = saved["version"]
        except Exception:
            result["library_error"] = "产物已生成，但角色版本保存失败；可稍后手动收录任务"
        repository.save(identifier, result)
    return 0 if result["status"] == "completed" else 1


class JobRuntime:
    def __init__(self, root: Path, config: Config):
        self.config = config
        self.repository = JobRepository(root)
        self.characters, self.presets = character_services(root)
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._active: str | None = None
        self._process_job = None
        self._inference_job = None
        self._started = 0.0
        self._lease = None

    def __enter__(self):
        self._lease = self.repository.exclusive()
        self._lease.__enter__()
        try:
            self._start_inference()
        except BaseException:
            self._lease.__exit__(*sys.exc_info())
            self._lease = None
            raise
        for state in self.repository.list():
            if state["status"] not in TERMINAL:
                state.update(status="failed", error="生成器进程已重启，原任务中断；不会自动重新提交")
                self.repository.save(state["id"], state)
        return self

    def __exit__(self, *args):
        try:
            if self._active:
                self.cancel(self._active)
        finally:
            self._release_process()
            if self._inference_job is not None:
                self._inference_job.close()
                self._inference_job = None
            if self._lease:
                self._lease.__exit__(*args)

    def _start_inference(self):
        if os.name != "nt" or not os.environ.get("AGENTSTRATA_GPT_SOVITS_CONFIG"):
            return
        from chatcopilot.core.windows_process import ProcessJob
        from chatcopilot.deployment.systemd import require_free_port
        from urllib.request import ProxyHandler, build_opener

        path = voice_jobs.voice_configuration()
        config = read_json(path)
        command = [config["python"], "-m", "agentstrata_live2d.voice_worker", "serve", "--config", str(path)]
        require_free_port(config["port"])
        job = ProcessJob()
        opener = build_opener(ProxyHandler({}))
        try:
            with (Path(config["runtime"]) / "inference.log").open("ab") as output:
                process = job.spawn(command, cwd=config["runtime"], env=voice_jobs.voice_environment(inference=True), output=output)
            try:
                deadline = time.monotonic() + 45
                while process.poll() is None and time.monotonic() < deadline:
                    try:
                        with opener.open(f"http://127.0.0.1:{config['port']}/health", timeout=1) as reply:
                            if reply.status == 200 and job.owns_listener(config["port"]):
                                self._inference_job = job
                                return
                    except OSError:
                        time.sleep(.2)
                raise GenerationError("本机 GPT-SoVITS 推理服务未启动，请查看 inference.log")
            finally:
                process.close()
        except BaseException:
            job.close()
            raise

    def _refresh(self):
        if self._process is None or self._active is None:
            return
        if self._process.poll() is None and time.monotonic() - self._started > self.config.timeout_seconds:
            state = self.repository.read(self._active)
            # The existing artwork deadline applies until the model is ready. Training
            # runs to completion or explicit cancellation in the owned process tree.
            if not (state.get("voice_job") and state.get("model_sha256")):
                self._terminate()
                if state["status"] not in TERMINAL:
                    state.update(status="failed", error="生成超过配置的时间限制；远端推理可能继续运行")
                    self.repository.save(self._active, state)
        if self._process.poll() is not None:
            state = self.repository.read(self._active)
            if state["status"] not in TERMINAL:
                state.update(status="failed", error="生成工作进程异常退出")
                self.repository.save(self._active, state)
            self._release_process()

    def _release_process(self):
        if self._process_job is not None:
            self._process_job.close()
            self._process_job = None
        if self._process is not None and os.name == "nt":
            self._process.close()
        self._process, self._active = None, None

    def _spawn(self, command, output):
        if os.name != "nt":
            return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                    start_new_session=True)
        from chatcopilot.core.windows_process import ProcessJob
        job = ProcessJob()
        try:
            process = job.spawn(command, cwd=self.repository.root, env=dict(os.environ), output=output)
        except BaseException:
            job.close()
            raise
        self._process_job = job
        return process

    def start(self, request: GenerationRequest, source: Path | None, *, character_id: str | None = None,
              retry_of: str | None = None, details: dict | None = None,
              identifier: str | None = None, voice_request: dict | None = None) -> dict:
        with self._lock:
            self._refresh()
            if self._process is not None:
                raise GenerationError("已有任务正在生成，请等待完成或取消")
            previous = self.repository.read(retry_of) if retry_of else {}
            checkpoints = previous.get("checkpoints", {})
            reuse_model = bool(previous.get("model_sha256"))
            voice_only = voice_request is not None and voice_request.get("base_version") is not None
            needs_image = not (reuse_model or voice_only or checkpoints) and (source is None or request.redraw)
            needs_layers = not (reuse_model or voice_only or "layers.psd" in checkpoints) and (
                source is None or source.suffix.lower() not in {".psd", ".zip"})
            if needs_image and not self.config.image_base_url:
                raise GenerationError("尚未配置绘图服务，可先上传立绘或分层素材")
            if needs_layers and not (self.config.decompose_url or self.config.decompose_space):
                raise GenerationError("尚未配置拆层服务，可先上传 PSD 或 PNG 图层 ZIP")
            if character_id:
                stored = self.characters.generation_profile(character_id)
                details = details or stored
            if voice_only or reuse_model:
                identifier = self.repository.create(asdict(request), source, identifier=identifier)
            else:
                identifier = service(self.repository.root, self.config).submit(request, source, identifier=identifier)
            root = self.repository.path(identifier)
            if character_id is None:
                record = self.characters.create(details or {"name": request.name, "identity": f"你是{request.name}。"})
                character_id, details = record["id"], record["draft"]
            state = self.repository.read(identifier)
            state["character_id"] = character_id
            if retry_of is not None:
                original = self.repository.path(retry_of)
                state["retry_of"] = retry_of
                for filename, sha256 in checkpoints.items():
                    if filename not in {"illustration.png", "layers.psd"} or digest(original / filename) != sha256:
                        raise GenerationError("原任务阶段摘要不匹配，无法继续")
                    copy_file(original / filename, root / filename)
                if checkpoints:
                    state["checkpoints"] = checkpoints
                if reuse_model:
                    if digest(original / "model.zip") != previous["model_sha256"]:
                        raise GenerationError("原任务模型摘要不匹配，无法继续")
                    preview = private_directory(root / "model")
                    copy_file(original / "model.zip", root / "model.zip")
                    copy_file(original / "model/preview.png", preview / "preview.png")
                    state.update(model_sha256=previous["model_sha256"], result=previous.get("result", {}))
            self.repository.save(identifier, state)
            if voice_request is not None:
                state["voice_job"] = True
                self.repository.save(identifier, state)
                write_json(root / "voice-request.json", voice_request)
                if retry_of:
                    old = read_json(original / "voice-request.json")
                    if old["sources"] == voice_request["sources"] and (original / "voice-training").is_dir():
                        write_json(root / "voice-resume.json", {"job": retry_of})
            write_json(root / "character-profile.json", details)
            write_json(root / "config.json", asdict(self.config))
            try:
                self.characters.preserve_job_input(identifier, character_id)
            except (OSError, GenerationError):
                state.update(status="failed", error="角色原稿保存失败，未启动生成；请检查本机存储")
                self.repository.save(identifier, state)
                raise GenerationError(state["error"]) from None
            try:
                with (root / "worker.log").open("wb") as output:
                    self._process = self._spawn(
                        [sys.executable, "-m", __package__ + ".cli", "_worker", "--root",
                         str(self.repository.root), "--job", identifier], output)
            except OSError:
                state = self.repository.read(identifier)
                state.update(status="failed", error="无法启动生成工作进程")
                self.repository.save(identifier, state)
                raise GenerationError("无法启动生成工作进程") from None
            self._active, self._started = identifier, time.monotonic()
            threading.Thread(target=self._watch, args=(identifier,), daemon=True).start()
            return self.repository.read(identifier)

    def retry(self, identifier: str, *, sources: list[dict] | None = None) -> dict:
        """Explicit retry creates a new job and reuses the saved illustration after decomposition failure."""
        with self._lock:
            self._refresh()
            state = self.repository.read(identifier)
            if state["status"] not in {"failed", "cancelled", "waiting_voice_input"}:
                raise GenerationError("仅可重试失败、已取消或等待音源的任务")
            root = self.repository.path(identifier)
            request = GenerationRequest.parse(read_json(root / "request.json"))
            source = next(root.glob("input.*"), None)
            illustration = root / "illustration.png"
            if not state.get("checkpoints") and state["stage"] == "decomposition" and illustration.is_file():
                image_info(illustration)
                source = illustration
                request = replace(request, redraw=False)
            voice_file = root / "voice-request.json"
            voice = read_json(voice_file) if voice_file.is_file() else None
            if sources is not None:
                if voice is None:
                    raise GenerationError("该任务没有语音阶段")
                voice["sources"] = voice_jobs._sources(sources)
            snapshot = root / "character-profile.json"
            return self.start(request, source, character_id=state.get("character_id"), retry_of=identifier,
                              details=read_json(snapshot) if snapshot.is_file() else None, voice_request=voice)

    def _watch(self, identifier: str):
        while True:
            time.sleep(1)
            with self._lock:
                if self._active != identifier:
                    return
                self._refresh()
                if self._active != identifier:
                    return

    def list(self) -> list[dict]:
        with self._lock:
            self._refresh()
            return [self._visible(state) for state in self.repository.list()]

    def _visible(self, state: dict) -> dict:
        if (state["status"] == "completed" and self._active == state["id"]
                and self._process is not None and self._process.poll() is None):
            return state | {"status": "running", "stage": "saving_character"}
        return state

    def get(self, identifier: str) -> dict:
        with self._lock:
            self._refresh()
            return self._visible(self.repository.read(identifier))

    def _terminate(self):
        if self._process is not None and self._process.poll() is None:
            if self._process_job is not None:
                self._process_job.close()
                self._process_job = None
                self._process.wait(timeout=3)
                return
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self._process.pid, signal.SIGKILL)
                self._process.wait(timeout=3)
            except ProcessLookupError:
                self._process.wait(timeout=3)

    def cancel(self, identifier: str) -> dict:
        with self._lock:
            self._refresh()
            state = self.repository.read(identifier)
            if state["status"] in TERMINAL:
                return state
            if self._active != identifier:
                raise GenerationError("任务不属于当前工作进程")
            self._terminate()
            state = self.repository.read(identifier)
            if state["status"] not in TERMINAL:
                state.update(status="cancelled", error="生成已取消；远端已提交的推理可能继续运行")
                self.repository.save(identifier, state)
            self._release_process()
            return state

    def artifact(self, identifier: str, name: str) -> Path:
        state = self.get(identifier)
        if state["status"] != "completed" and not state.get("model_sha256"):
            raise GenerationError("任务尚未完成模型校验")
        if name not in {"model.zip", "preview.png"}:
            raise GenerationError("未知产物")
        root = self.repository.path(identifier)
        return root / ("model/preview.png" if name == "preview.png" else "model.zip")


def configured_runtime(root: Path, config_path: Path) -> JobRuntime:
    load_environment(config_path.with_name("runtime.env"))
    return JobRuntime(root, Config.load(config_path))


def character_services(jobs: Path):
    from .character_repository import CharacterRepository
    from .character_service import CharacterService
    from .github_presets import GitHubPresets
    from .preset_service import PresetService

    characters = CharacterService(CharacterRepository(jobs.parent / "characters"), JobRepository(jobs))
    repository = os.environ.get("AGENTSTRATA_LIVE2D_PRESET_REPOSITORY", "")
    source = GitHubPresets(repository, os.environ.get("AGENTSTRATA_LIVE2D_PRESET_TOKEN", "")) if repository else None
    namespace = hashlib.sha256(repository.encode()).hexdigest()[:16]
    presets = PresetService(characters, characters.repository.root / ".catalog" / namespace, source)
    return characters, presets


def configured_character_services(root: Path, config_path: Path | None = None):
    if config_path is not None:
        load_environment(config_path.with_name("runtime.env"))
    return character_services(root)


def configured_character_target() -> Path:
    value = os.environ.get("AGENTSTRATA_LIVE2D_VTUBER_ROOT", "")
    if not value or not Path(value).is_absolute():
        raise GenerationError("请在本机配置 AGENTSTRATA_LIVE2D_VTUBER_ROOT，然后停止客户端再应用角色")
    return Path(value)
