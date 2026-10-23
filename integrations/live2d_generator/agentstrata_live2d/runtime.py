"""Assembly and one bounded child process. No model is loaded in the host process."""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .config import Config, load_environment
from .repository import JobRepository, write_json
from .service import GenerationService
from .types import GenerationError, GenerationRequest, TERMINAL


def service(root: Path, config: Config) -> GenerationService:
    from .providers import ImagesAPI, SeeThroughHTTP, SeeThroughSpace
    from .rigging import AutomaticRigger

    return GenerationService(JobRepository(root), ImagesAPI(config),
                             SeeThroughSpace(config) if config.decompose_space else SeeThroughHTTP(config),
                             AutomaticRigger(), config.timeout_seconds)


def worker(root: Path, identifier: str) -> int:
    repository = JobRepository(root)
    config = Config.load(repository.path(identifier) / "config.json")
    result = service(root, config).run(identifier)
    return 0 if result["status"] == "completed" else 1


class JobRuntime:
    def __init__(self, root: Path, config: Config):
        self.config = config
        self.repository = JobRepository(root)
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._active: str | None = None
        self._started = 0.0
        self._lease = None

    def __enter__(self):
        self._lease = self.repository.exclusive()
        self._lease.__enter__()
        for state in self.repository.list():
            if state["status"] not in TERMINAL:
                state.update(status="failed", error="生成器进程已重启，原任务中断；不会自动重新提交")
                self.repository.save(state["id"], state)
        return self

    def __exit__(self, *args):
        if self._active:
            self.cancel(self._active)
        if self._lease:
            self._lease.__exit__(*args)

    def _refresh(self):
        if self._process is None or self._active is None:
            return
        if self._process.poll() is None and time.monotonic() - self._started > self.config.timeout_seconds:
            self._terminate()
            state = self.repository.read(self._active)
            if state["status"] not in TERMINAL:
                state.update(status="failed", error="生成超过配置的时间限制；远端推理可能继续运行")
                self.repository.save(self._active, state)
        if self._process.poll() is not None:
            state = self.repository.read(self._active)
            if state["status"] not in TERMINAL:
                state.update(status="failed", error="生成工作进程异常退出")
                self.repository.save(self._active, state)
            self._process, self._active = None, None

    def start(self, request: GenerationRequest, source: Path | None) -> dict:
        with self._lock:
            self._refresh()
            if self._process is not None:
                raise GenerationError("已有任务正在生成，请等待完成或取消")
            needs_image = source is None or request.redraw
            needs_layers = source is None or source.suffix.lower() not in {".psd", ".zip"}
            if needs_image and not self.config.image_base_url:
                raise GenerationError("尚未配置绘图服务，可先上传立绘或分层素材")
            if needs_layers and not (self.config.decompose_url or self.config.decompose_space):
                raise GenerationError("尚未配置拆层服务，可先上传 PSD 或 PNG 图层 ZIP")
            identifier = service(self.repository.root, self.config).submit(request, source)
            root = self.repository.path(identifier)
            write_json(root / "config.json", asdict(self.config))
            try:
                with (root / "worker.log").open("wb") as output:
                    self._process = subprocess.Popen(
                        [sys.executable, "-m", __package__ + ".cli", "_worker", "--root",
                         str(self.repository.root), "--job", identifier],
                        stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
            except OSError:
                state = self.repository.read(identifier)
                state.update(status="failed", error="无法启动生成工作进程")
                self.repository.save(identifier, state)
                raise GenerationError("无法启动生成工作进程") from None
            self._active, self._started = identifier, time.monotonic()
            threading.Thread(target=self._watch, args=(identifier,), daemon=True).start()
            return self.repository.read(identifier)

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
            return self.repository.list()

    def get(self, identifier: str) -> dict:
        with self._lock:
            self._refresh()
            return self.repository.read(identifier)

    def _terminate(self):
        if self._process is not None and self._process.poll() is None:
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
            self._process, self._active = None, None
            return state

    def artifact(self, identifier: str, name: str) -> Path:
        state = self.get(identifier)
        if state["status"] != "completed":
            raise GenerationError("任务尚未成功完成")
        if name not in {"model.zip", "preview.png"}:
            raise GenerationError("未知产物")
        root = self.repository.path(identifier)
        return root / ("model/preview.png" if name == "preview.png" else "model.zip")


def configured_runtime(root: Path, config_path: Path) -> JobRuntime:
    load_environment(config_path.with_name("runtime.env"))
    return JobRuntime(root, Config.load(config_path))
