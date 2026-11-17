"""Host-owned settings and observation projection for one application."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import threading
from collections import OrderedDict

from chatcopilot.contracts.application_management import (
    ApplicationReadParams, ApplicationReadResult, ApplicationWriteParams, ApplicationWriteResult,
)
from chatcopilot.core.application_settings import ApplicationSettings, revision
from chatcopilot.core.inspection import public_configuration
from chatcopilot.core.model_settings import ModelSettingsStore, settings_path
from chatcopilot.gateway.observation_store import ObservationStore
from chatcopilot.gateway import observation_queries as queries
from chatcopilot.gateway.observation_queries import RunFilter


class ApplicationManagement:
    def __init__(self, runtime, state_root: Path, environment, *, installation_id: str = ""):
        self.runtime = runtime
        self.environment = dict(environment)
        self.store = ApplicationSettings(state_root, runtime.instance_id)
        self.observations = ObservationStore(state_root)
        self.installation_id = installation_id or runtime.instance_id
        self._transfers = OrderedDict()
        self._transfer_lock = threading.Lock()

    def settings(self) -> dict:
        document = ModelSettingsStore(settings_path(self.environment), defaults_path=settings_path(self.environment).with_suffix(".defaults.json")).view()
        model_context = document["revision"]
        base = self.runtime.prompt_profile
        prompts = self.store.read("prompts", {"identity": base.identity, "response_style": base.response_style})
        model = self.store.read("model", {"profile": document["bindings"].get(
            self.environment.get("CHATCOPILOT_LLM_BINDING", "chat"), "")})
        current = {"prompts": {"values": prompts, "revision": revision([prompts, ""])},
                   "model": {"values": model, "revision": revision([model, model_context])}}
        loaded, fresh = {}, False
        if self.observations.database.exists():
            meta = self.observations.meta("loaded_configuration") or {}
            if meta.get("config_id"):
                snapshot = self.observations.configuration(meta["config_id"]) or {}
                loaded = snapshot.get("application_settings", {})
                status = self.observations.meta("runtime_status") or {}
                stopped = (status.get("state") == "stopped" and meta.get("generation") is not None
                           and status.get("generation") == meta["generation"]
                           and status.get("observed_at", 0) >= meta.get("observed_at", 0))
                fresh = 0 <= time.time() - meta.get("observed_at", 0) < 15 and not stopped
        for section in current.values():
            section["status"] = "unknown"
        for name, section in current.items():
            if fresh and name in loaded:
                section["status"] = "applied" if loaded[name] == section["revision"] else "pending"
        return {**current, "models": [{"id": key, "model": value["model"], "connection": value["connection"]}
                for key, value in document["profiles"].items()]}

    def write(self, params: ApplicationWriteParams) -> ApplicationWriteResult:
        current = self.settings()
        default = current[params.section]["values"]
        context = ""
        if params.section == "model":
            document = ModelSettingsStore(settings_path(self.environment), defaults_path=settings_path(self.environment).with_suffix(".defaults.json")).view()
            if params.values.get("profile") not in document["profiles"]:
                raise ValueError("模型方案不存在，请先在模型与连接中配置")
            context = document["revision"]
        saved = self.store.save(params.section, dict(params.values), params.revision, default=default, context=context)
        return ApplicationWriteResult(saved)

    def project(self, operation: str, args: dict):
        store = self.observations
        if operation == "identity":
            return {"id": self.installation_id, "name": self.runtime.display_name or self.runtime.instance_id,
                    "bot_id": self.runtime.bot_id, "runtime_kind": "gateway",
                    "type": "companion" if self.runtime.bot_id == "live2d-desktop" else "desktop"}
        if operation == "settings":
            return self.settings()
        if operation in {"history", "metrics"}:
            filters = RunFilter(**args)
            return queries.history(store, filters) if operation == "history" else queries.metrics(store, filters)
        run_id = args.get("run_id", "")
        if operation == "inspection":
            from chatcopilot.botspec.inspection import expected_configuration
            current = expected_configuration(self.runtime.source_path, self.environment,
                                             home=Path.home(), deployment_defaults=False)
            meta = store.meta("loaded_configuration") or {}
            loaded = store.configuration(meta["config_id"]) if meta.get("config_id") else None
            run = queries.detail(store, run_id) if run_id else None
            config_id = run["run"].get("config_id") if run else None
            if args.get("event_seq") is not None:
                events = queries.events(store, run_id, after=args["event_seq"] - 1, limit=1)["observations"]
                event = next((e for e in events if e["seq"] == args["event_seq"]), None)
                if event is None:
                    raise ValueError("阶段不属于当前任务")
                config_id = event["data"].get("configuration_id")
            return public_configuration({"current": current, "loaded": loaded,
                "execution": store.configuration(config_id) if config_id else None,
                "loaded_meta": meta, "run_id": run_id or None, "errors": [], "generated_at": time.time(),
                "configuration_status": "unknown", "configuration_status_reason": "请在应用配置中核对已保存与已加载设置"})
        run = queries.detail(store, run_id)
        if run is None:
            raise ValueError("任务不存在")
        if operation == "run":
            return run
        if operation == "events":
            return queries.events(store, run_id, after=args.get("after", 0), limit=args.get("limit", 200))
        if operation == "body":
            return store.body(run_id, args["body_id"])
        if operation == "trace":
            from chatcopilot.core.trace_archive import TraceArchive
            reference = store.meta("trace:" + run_id) or {"capture_state": "not_recorded"}
            if not reference.get("trace_ref"):
                return {**reference, "spans": []}
            archive = TraceArchive(store.root / "traces")
            kwargs = {"source": {"kind": "robot_task", "run_id": run_id}, "sha256": reference["sha256"]}
            if args.get("span_id"):
                return archive.step(reference["trace_ref"], args["span_id"], **kwargs)
            return archive.summary(reference["trace_ref"], after=args.get("after", 0), **kwargs)
        raise ValueError("不支持的应用查询")

    def read(self, params: ApplicationReadParams) -> ApplicationReadResult:
        # Short-lived transfer snapshots preserve one read across Gateway frames.
        # They never become history or an observation source of their own.
        query = revision([params.operation, dict(params.arguments)])
        if params.offset:
            with self._transfer_lock:
                record = self._transfers.get(params.snapshot)
            if record is None or record[0] != query or time.monotonic() - record[1] > 30:
                raise ValueError("观测传输已过期，请重新读取")
            content = record[2]
            digest = params.snapshot
        else:
            content = json.dumps(self.project(params.operation, dict(params.arguments)), ensure_ascii=True, sort_keys=True)
            if len(content) > 16 * 1024 * 1024:
                raise ValueError("观测正文过大，请使用分项记录")
            digest = hashlib.sha256(content.encode()).hexdigest()
            if len(content) > 16000:
                with self._transfer_lock:
                    self._transfers[digest] = (query, time.monotonic(), content)
                    self._transfers.move_to_end(digest)
                    while len(self._transfers) > 4:
                        self._transfers.popitem(last=False)
        return ApplicationReadResult(content[params.offset:params.offset + 16000], digest, len(content))

    def loaded_revisions(self):
        settings = self.settings()
        return {key: settings[key]["revision"] for key in ("prompts", "model")}
