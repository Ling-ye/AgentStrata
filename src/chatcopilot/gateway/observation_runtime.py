"""Runtime-owned observation projection, recovery and retention lifecycle."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import copy
from importlib.metadata import PackageNotFoundError, version
import logging
import os
import threading
import time
from typing import Any, Iterator, Mapping

from chatcopilot.botspec.inspection import configuration_projection
from chatcopilot.core.inspection import fingerprint, plain
from chatcopilot.core.observation_context import observation_scope
from chatcopilot.core.runtime_observation import current_runtime_stage
from .observation_store import ObservationStore, decoded

_LOG = logging.getLogger(__name__)
_ACTIVE: ContextVar[tuple[Any, str] | None] = ContextVar("gateway_observed_run", default=None)


class _RunLogHandler(logging.Handler):
    def __init__(self, recorder: ObservationRecorder) -> None:
        super().__init__(logging.INFO)
        self.recorder = recorder

    def emit(self, record: logging.LogRecord) -> None:
        active = _ACTIVE.get()
        if not active or active[0] is not self.recorder or record.name.startswith("chatcopilot.gateway.observation"):
            return
        try:
            stage = current_runtime_stage()
            stage_data = ({"flow_version": 1, "runtime_layer": stage.runtime_layer,
                           "stage_span_id": stage.span_id, "trace_id": stage.trace_id}
                          if stage is not None else {})
            self.recorder.record(active[1], {"kind": "log", "layer": "application", "entity_id": "workspace:instance",
                "status": "failed" if record.levelno >= logging.ERROR else "recorded", "created_at": record.created,
                "data": {"level": record.levelname, "logger": record.name, **stage_data}},
                body={"message": record.getMessage(), "level": record.levelname, "logger": record.name})
        except Exception:
            pass


class ObservationRecorder:
    def __init__(self, state_store: Any, generation: int, *, configuration: dict[str, Any] | None = None,
                 snapshot_provider: Any = None) -> None:
        self.state_store = state_store
        self.generation = generation
        self.store = ObservationStore(state_store.root, writable=True)
        self.configuration = configuration or {"layers": [], "entities": []}
        self.snapshot_provider = snapshot_provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handler = _RunLogHandler(self)
        self._lock = threading.RLock()
        self.ready_at: float | None = None
        try:
            self.version = version("agentstrata")
        except PackageNotFoundError:
            self.version = None
        self.config_id = self.store.put_configuration(self.configuration)
        state_store.observation_recorder = self
        self.reconcile()
        self.refresh()
        self.store.expire()

    def start(self) -> None:
        self.ready_at = time.time()
        self.refresh()
        logging.getLogger().addHandler(self._handler)
        self._thread = threading.Thread(target=self._maintain, name="gateway-observation", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        logging.getLogger().removeHandler(self._handler)
        self._handler.close()
        try:
            self.store.set_meta("runtime_status", {"state": "stopped", "observed_at": time.time(), "generation": self.generation})
        except Exception:
            _LOG.warning("Observation shutdown status unavailable")

    def _maintain(self) -> None:
        while not self._stop.wait(5):
            try:
                self.refresh()
                self.store.expire()
            except Exception:
                _LOG.warning("Observation maintenance unavailable")

    def refresh(self) -> None:
        with self._lock:
            with self.state_store._read_connection() as connection:
                self.state_store._assert_generation(connection, self.generation)
                audit = _rows(connection, "SELECT allowed,code,policy_version,observed_at FROM authorization_decisions "
                              "ORDER BY observed_at DESC,decision_id DESC LIMIT 101", ())
            if self.snapshot_provider:
                self.configuration = self.snapshot_provider()
            for entity in self.configuration.get("entities", []):
                if entity["id"] == "service:instance":
                    entity["runtime"] = {"version": self.version, "ready_at": self.ready_at, "pid": os.getpid()}
            self.config_id = self.store.put_configuration(self.configuration)
            self.store.set_meta("loaded_configuration", {"config_id": self.config_id, "generation": self.generation,
                                                        "observed_at": time.time()})
            self.store.set_meta("instance_audit", {"audit": audit[:100], "audit_truncated": len(audit) > 100})

    def reconcile(self, run_id: str | None = None) -> None:
        # The runtime reads its own authority; Console never constructs this state owner.
        with self._lock, self.state_store._read_connection() as connection:
            self.state_store._assert_generation(connection, self.generation)
            cursor = connection.execute(
                "SELECT r.run_id,r.state,r.error_code,r.created_at,r.started_at,r.finished_at,r.updated_at,r.generation,"
                "r.session_id,s.channel,s.conversation_kind FROM runs r JOIN sessions s ON s.session_id=r.session_id "
                + ("WHERE r.run_id=?" if run_id else "ORDER BY r.created_at"), (run_id,) if run_id else ())
            names = [column[0] for column in cursor.description]
            for row in cursor:
                run = dict(zip(names, row))
                selected = run["run_id"]
                session_id = run.pop("session_id")
                run["receipts"] = _rows(connection,
                    "SELECT d.receipt_id,d.outbound_id,d.stage,d.observed_at,d.error_code FROM delivery_receipts d "
                    "JOIN outbox o ON o.outbound_id=d.outbound_id WHERE o.run_id=? AND o.session_id=? ORDER BY d.rowid LIMIT 1001",
                    (selected, session_id))
                run["outbox"] = _rows(connection, "SELECT outbound_id,state,error_code,created_at,updated_at FROM outbox "
                                     "WHERE run_id=? AND session_id=? ORDER BY rowid LIMIT 1001", (selected, session_id))
                run["approvals"] = _rows(connection, "SELECT operation,state,accepted,created_at,decided_at FROM approvals "
                                        "WHERE run_id=? AND session_id=? ORDER BY created_at LIMIT 1001", (selected, session_id))
                overflow = any(len(run[key]) > 1000 for key in ("receipts", "outbox", "approvals"))
                for key in ("receipts", "outbox", "approvals"):
                    run[key] = run[key][:1000]
                self.store.project_run(run)
                if overflow:
                    with self.store.connection(write=True) as observed:
                        observed.execute("UPDATE runs SET capture_state='truncated' WHERE run_id=? AND capture_state!='capture_failed'", (selected,))
                if run_id and run["state"] == "accepted":
                    self.store.bind_run(selected, config_id=self.config_id, backend=str(self.configuration.get("backend", "")),
                                        model=str(self.configuration.get("model", "")))
                if run_id and run["finished_at"] is not None and run["finished_at"] + 30 * 86400 > time.time():
                    result = connection.execute("SELECT result_json FROM runs WHERE run_id=?", (selected,)).fetchone()
                    if result and result[0]:
                        self.store.attach_body(selected, "result", decoded(result[0]))

    def prepare(self, run_id: str, request: Any) -> None:
        self.refresh()
        role = request.principal.role.value
        self.store.bind_run(run_id, config_id=self.config_id, role=role,
                            backend=str(self.configuration.get("backend", "")), model=str(self.configuration.get("model", "")))
        self.store.attach_body(run_id, "input", {"text": request.canonical_text})

    def accepted(self, run_id: str, text: str, role: str) -> None:
        self.store.bind_run(run_id, config_id=self.config_id, role=role,
                            backend=str(self.configuration.get("backend", "")), model=str(self.configuration.get("model", "")))
        self.store.attach_body(run_id, "input", {"text": text})

    @contextmanager
    def scope(self, run_id: str) -> Iterator[None]:
        token = _ACTIVE.set((self, run_id))
        try:
            with observation_scope(lambda kind, data: self.host_event(run_id, kind, data)):
                yield
        finally:
            _ACTIVE.reset(token)

    def host_event(self, run_id: str, kind: str, data: dict[str, Any]) -> None:
        data = dict(data)
        stage = current_runtime_stage()
        if kind == "runtime_stage":
            body = data.pop("body", None)
            phase = data.pop("phase")
            layer = data["runtime_layer"]
            content = body.get("input" if phase == "start" else "output") if isinstance(body, dict) else None
            body_state = content.get("capture_state") if isinstance(content, dict) else None
            entity = {"channel": "channel:qq", "gateway": "gateway:instance",
                      "application": "workspace:instance", "agent": "agent:main"}[layer]
            self.record(run_id, {"kind": "RuntimeStageStarted" if phase == "start" else "RuntimeStageFinished",
                "layer": layer, "entity_id": entity, "phase": phase,
                "status": data.pop("status"), "created_at": data.pop("observed_at"), "data": data,
                "body_state": body_state}, body=body)
            return
        if stage is not None:
            data.update(flow_version=1, runtime_layer=stage.runtime_layer,
                        stage_span_id=stage.span_id)
            data.setdefault("trace_id", stage.trace_id)
        name = data.get("name", "")
        if kind == "tool_authorization":
            self.record(run_id, {"kind": kind, "layer": "authorization", "entity_id": f"tool:{name}",
                "refs": ["policy:instance", f"tool:{name}"], "status": "succeeded" if data.get("allowed") else "failed",
                "trace_id": data.get("trace_id"), "span_id": data.get("span_id"), "data": data}, body=data)
        elif kind == "session_registry":
            with self.store.connection() as connection:
                row = connection.execute("SELECT config_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            config = self.store.configuration(row[0]) if row and row[0] else copy.deepcopy(self.configuration)
            config = config or {}
            entities = {item["id"]: item for item in config.get("entities", [])}
            for tool in data.get("tools", []):
                name = tool["name"]
                refs = [f"pack:{tool['pack']}"] if tool.get("pack") else []
                if tool.get("mcp_server_id"):
                    refs.append(f"mcp:{tool['mcp_server_id']}")
                entities[f"tool:{name}"] = {"id": f"tool:{name}", "layer": "capability", "name": name,
                    "configured": True, "loaded": True, "connected": None, "available": tool["available"],
                    "refs": refs, "config": {key: value for key, value in tool.items() if key != "available"}}
                config.setdefault("tool_bindings", {})[name] = refs
            config["entities"] = list(entities.values())
            key = self.store.put_configuration(config)
            with self.store.connection(write=True) as connection:
                connection.execute("UPDATE runs SET config_id=? WHERE run_id=?", (key, run_id))
        elif kind == "session_capabilities":
            with self.store.connection() as connection:
                row = connection.execute("SELECT config_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            config = self.store.configuration(row[0]) if row and row[0] else copy.deepcopy(self.configuration)
            config = config or {}
            allowed = set(data.get("tools", []))
            for entity in config.get("entities", []):
                if entity["id"].startswith("tool:"):
                    entity["available"] = entity["id"][5:] in allowed
                if entity["id"] == "policy:instance":
                    entity["runtime"] = {"role": data.get("role"), "workspace_scope": data.get("workspace_scope")}
            config["session"] = data
            key = self.store.put_configuration(config)
            with self.store.connection(write=True) as connection:
                connection.execute("UPDATE runs SET config_id=? WHERE run_id=?", (key, run_id))
            self.record(run_id, {"kind": kind, "layer": "agent", "entity_id": "agent:main", "status": "succeeded",
                                "data": {"tool_count": len(allowed), "role": data.get("role"), "configuration_id": key,
                                         **{name: data[name] for name in ("flow_version", "runtime_layer", "stage_span_id", "trace_id") if name in data}}})

    def record(self, run_id: str, event: dict[str, Any], *, body: Any = None, context: bool = False) -> None:
        try:
            data = event.get("data", {})
            for key in ("source", "target", "parent_span_id"):
                if key in event:
                    data.setdefault(key, event[key])
            with self.store.connection() as connection:
                recorded = connection.execute("SELECT config_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if recorded and recorded[0]:
                data["configuration_id"] = recorded[0]
            name = data.get("name")
            if name:
                with self.store.connection() as connection:
                    row = connection.execute("SELECT config_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
                config = self.store.configuration(row[0]) if row and row[0] else self.configuration
                bindings = (config or {}).get("tool_bindings", {}).get(name, [])
                event["refs"] = list(dict.fromkeys([event.get("entity_id"), *event.get("refs", []), *bindings]))
            if data.get("model"):
                with self.store.connection() as connection:
                    row = connection.execute("SELECT config_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
                config = self.store.configuration(row[0]) if row and row[0] else copy.deepcopy(self.configuration)
                if config is not None:
                    identity = f"model:{data['model']}"
                    config["entities"] = [item for item in config.get("entities", []) if item["id"] != identity]
                    config["entities"].append({"id": identity, "name": data["model"], "layer": "agent", "configured": True,
                        "loaded": True, "available": True, "connected": None,
                        "config": {"model": data["model"], "backend": data.get("backend"), "model_selection": data.get("model_selection")}})
                    key = self.store.put_configuration(config)
                    data["configuration_id"] = key
                    with self.store.connection(write=True) as connection:
                        connection.execute("UPDATE runs SET config_id=? WHERE run_id=?", (key, run_id))
                        if event["kind"] == "LlmCallStarted" and not data.get("depth"):
                            connection.execute("UPDATE runs SET model=? WHERE run_id=? AND NOT EXISTS "
                                "(SELECT 1 FROM events WHERE run_id=? AND kind='LlmCallStarted' "
                                "AND COALESCE(json_extract(metadata,'$.depth'),0)=0)", (data["model"], run_id, run_id))
            event["data"] = data
            self.store.append(run_id, event, body=body, context=context)
        except Exception:
            _LOG.warning("Observation event unavailable")
            try:
                with self.store.connection(write=True) as connection:
                    connection.execute("UPDATE runs SET capture_state='capture_failed' WHERE run_id=?", (run_id,))
            except Exception:
                pass


def _rows(connection: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = connection.execute(sql, params)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor]


def runtime_configuration(runtime: Any, agent: Any, environment: Mapping[str, str]) -> dict[str, Any]:
    config = configuration_projection(runtime.spec, mcp=runtime.mcp_servers, skills=runtime.skills,
                                      rag=runtime.rag_sources, environment=environment)
    config["backend"] = str(agent.agent_backend)
    config["model"] = str(agent.runtime_config.routing.code_model if agent.agent_backend == "codex" else agent.runtime_config.llm.model)
    config["configuration_revision"] = fingerprint(configuration_projection(runtime.spec, mcp=runtime.mcp_servers,
        skills=runtime.skills, rag=runtime.rag_sources, environment=environment))
    config["tool_bindings"] = {}
    entities = {item["id"]: item for item in config["entities"]}
    for identity, entity in entities.items():
        entity["loaded"] = None
        if identity in {"agent:main", "config:instance", "prompts:instance", "service:instance"} or identity.startswith("skill:"):
            entity["loaded"] = entity["configured"]
        if identity.startswith("model-slot:"):
            entity["loaded"] = True
    entities["prompts:instance"]["config"]["content_hash"] = fingerprint(plain(runtime.prompt_profile))
    entities["model-slot:chat"]["runtime"] = plain(agent.runtime_config.llm)
    entities["model-slot:code"]["runtime"] = plain(agent.runtime_config.routing)
    research_config = getattr(getattr(agent, "research_llm", None), "config", None)
    if research_config is not None:
        entities["model-slot:research"]["runtime"] = plain(research_config)
    if agent.tool_registry is not None:
        snapshot = agent.tool_registry.snapshot(tool_packs=agent.tool_packs, exclude_tools=agent.exclude_tools,
                                               require_all_selected=False)
        materialized_packs = {source.pack_id for source in snapshot.sources.values()}
        for identity, entity in entities.items():
            if identity.startswith("pack:"):
                entity["loaded"] = identity[5:] in materialized_packs
        for tool in snapshot.tools:
            source = snapshot.sources[tool.name]
            references = [f"pack:{source.pack_id}"] if source.pack_id else []
            server_id = tool.metadata.get("mcp_server_id")
            if server_id:
                references.append(f"mcp:{server_id}")
            for kind in ("subagent", "workflow"):
                if tool.metadata.get(kind):
                    references.append(f"{kind}:{tool.metadata[kind]}")
            config["tool_bindings"][tool.name] = references
            config["entities"].append(
                {
                    "id": f"tool:{tool.name}",
                    "layer": "capability",
                    "name": tool.name,
                    "configured": True,
                    "loaded": True,
                    "connected": None,
                    "available": None,
                    "refs": references,
                    "config": {
                        "pack": source.pack_id,
                        "provider": source.provider_id,
                        "access": plain(tool.access),
                        "private_chat_only": tool.metadata.get("private_chat_only"),
                        "parameters": plain(tool.input_schema),
                    },
                }
            )
    if agent.mcp_provider is not None:
        for status in agent.mcp_provider.status():
            entity = entities.get(f"mcp:{status['id']}")
            if entity:
                entity.update(connected=status["running"], loaded=bool(status["tools_count"]), runtime=status)
    return config
