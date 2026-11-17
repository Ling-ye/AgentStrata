"""Resolve an application identity to its existing observation/settings owner."""
from __future__ import annotations

from pathlib import Path
import os

from chatcopilot.botspec.runtime import load_runtime_context
from chatcopilot.contracts.application_management import ApplicationWriteParams
from chatcopilot.gateway.rpc_validation import parse_request_params
from chatcopilot.host.application_management import ApplicationManagement
from console.control import gateway_observability as observations
from console.control.native_applications import NativeApplications


class Management:
    def __init__(self, applications):
        self.apps = applications

    def instance(self, ident):
        row = self.apps.require(ident)
        inst = next((value for value in self.apps._discover() if value.instance_id == row["instance_id"]), None)
        if inst is None:
            raise KeyError(ident)
        return inst

    def local(self, ident):
        row = self.apps.require(ident, "settings")
        if row["source"] != "repository":
            raise PermissionError("此应用的配置由其客户端管理")
        inst = self.instance(ident)
        root, values = observations._context(inst)
        if root is None:
            raise ValueError("应用尚未配置运行目录，请先完成实例准备")
        return ApplicationManagement(load_runtime_context(Path(inst.bot_spec)), root, {**os.environ, **values})

    def read(self, ident, operation, arguments=None):
        row = self.apps.require(ident)
        args = arguments or {}
        parsed = parse_request_params("application.read", {"operation": operation, "arguments": args})
        if row["source"] == "native":
            return NativeApplications(self.apps.state_dir).read(ident, parsed.operation, dict(parsed.arguments))
        if operation == "settings":
            return self.local(ident).settings()
        inst = self.instance(ident)
        if operation in {"history", "metrics"}:
            from chatcopilot.gateway.observation_queries import RunFilter
            filters = RunFilter(**args)
            return observations.snapshot(inst, filters=filters) if operation == "history" else observations.metrics(inst, filters)
        if operation == "run":
            return observations.snapshot(inst, args["run_id"])
        if operation == "events":
            return observations.event_page(inst, args["run_id"], args.get("after", 0), args.get("limit", 200))
        if operation == "body":
            return observations.body(inst, args["run_id"], args["body_id"])
        if operation == "inspection":
            result = observations.inspection(inst, **args)
            if row["capabilities"]["settings"]:
                settings = self.local(ident).settings()
                states = {settings[key]["status"] for key in ("model", "prompts")}
                if "pending" in states:
                    result.update(configuration_status="pending", pending_changes=True,
                                  configuration_status_reason="应用用户设置存在尚未加载的变更")
                elif "unknown" in states and result["configuration_status"] == "applied":
                    result.update(configuration_status="unknown", pending_changes=False,
                                  configuration_status_reason="尚未核对应用用户设置的加载状态")
            return result
        if operation == "trace":
            return observations.trace_record(inst, **args)
        raise ValueError("不支持的应用查询")

    def write(self, ident, section, expected, values):
        row = self.apps.require(ident, "settings")
        parsed = parse_request_params("application.settings", {"section": section, "revision": expected, "values": values})
        if row["source"] == "native":
            return NativeApplications(self.apps.state_dir).write(ident, section, expected, values)
        result = self.local(ident).write(ApplicationWriteParams(parsed.section, parsed.revision, parsed.values))
        return {"revision": result.revision}

    def source(self, ident):
        row = self.apps.require(ident, "observation")
        if row["source"] != "native":
            return self.instance(ident).to_dict()
        self.read(ident, "identity")
        return {"instance_id": ident, "display_name": row["name"], "runtime_kind": "gateway",
                "origin": {"kind": "native", "installation_id": ident}, "platform": "desktop", "is_deployed": True}
