"""Explicit QQ development, scoped to a controlled application source and service."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from chatcopilot.core.model_settings import worker_profile
from chatcopilot.harness.improvement_types import ImprovementRequest
from chatcopilot.harness.models import ACTIVE, TERMINAL, RepairOptions


class Development:
    def __init__(self, applications):
        self.applications = applications

    def selected(self, ident):
        row = self.applications.require(ident)
        if row["source"] != "studio" or row["type"] != "qq_official":
            raise PermissionError("此来源不支持标准 QQ 源码开发")
        return row, self.applications.manager()

    def status(self, ident):
        row, manager = self.selected(ident)
        return manager.source_status(row["project_id"])

    def prepare(self, ident):
        row, manager = self.selected(ident)
        with self.applications._operation_lock(row):
            return manager.prepare_source(row["project_id"])

    @staticmethod
    def view(task, project):
        if task.get("source", {}).get("bot_id") != "qq-app-" + project:
            raise KeyError("任务不属于当前应用")
        # Raw worker output, filesystem paths and model configuration never leave this BFF.
        status = task.get("status", "unknown")
        known = ACTIVE | TERMINAL
        return {"task_id": task["task_id"], "status": status if status in known else "processing",
                "can_cancel": status in ACTIVE,
                "can_install": status == "fixed" and bool(task.get("accepted_candidate"))
                and not task.get("delivery_cancel_requested", False)}

    def tasks(self, ident, page=1):
        row, manager = self.selected(ident)
        if manager.source_status(row["project_id"])["state"] == "unprepared":
            return {"tasks": [], "page": page, "total": 0}
        host = manager.source_controller(row["project_id"])
        result = host.list(page=page, limit=20, bot_id="qq-app-" + row["project_id"])
        return {"tasks": [self.view(task, row["project_id"]) for task in result["tasks"]],
                "page": page, "total": result["total"]}

    def get(self, ident, task_id):
        row, manager = self.selected(ident)
        host = manager.source_controller(row["project_id"], task_id=task_id)
        return self.view(host.get(task_id), row["project_id"])

    def start(self, ident, request_id, goal):
        row, manager = self.selected(ident)
        request_id = UUID(str(request_id)).hex
        goal = goal.strip()
        if not goal or len(goal) > 16000:
            raise ValueError("请输入具体的开发目标")
        with self.applications._operation_lock(row):
            manager._require_stopped(row["project_id"])
            state = manager.source_status(row["project_id"])
            if state["state"] not in {"prepared", "installed"} or not state.get("definition_matches"):
                raise ValueError("请先准备与当前配置一致的应用源码")
            # Persist intent before starting any worker, so a lost response cannot move
            # the same request to a different application, goal or frozen baseline.
            with self.applications.studio().db.connect(write=True) as db:
                db.execute("CREATE TABLE IF NOT EXISTS application_development_requests "
                           "(id TEXT PRIMARY KEY, project TEXT NOT NULL, goal TEXT NOT NULL, source TEXT NOT NULL)")
                previous = db.execute("SELECT project,goal,source FROM application_development_requests WHERE id=?",
                                      (request_id,)).fetchone()
                identity = (row["project_id"], goal, state["source_digest"])
                if previous and tuple(previous) != identity:
                    raise ValueError("同一开发请求不能更换应用、目标或源码版本")
                db.execute("INSERT OR IGNORE INTO application_development_requests VALUES (?,?,?,?)",
                           (request_id, *identity))
            host = manager.source_controller(row["project_id"])
            cfg, frozen = worker_profile("", environment={**os.environ, **host.settings})
            from chatcopilot.external_tools.codex_cli.auth_cli import status_lanes
            if not cfg.credential_root or status_lanes(Path(cfg.credential_root), "worker")[0].state != "ready":
                raise ValueError("开发账号尚未就绪，请先连接开发模型")
            host = manager.source_controller(row["project_id"], start_eval=True)
            bot = "qq-app-" + row["project_id"]
            scope = (f"bots/{bot}/identity.md", f"bots/{bot}/response.md")
            request = ImprovementRequest("improve", bot, goal, scope, suite_id="agentstrata-agent-tasks-v1")
            options = RepairOptions(cfg.model, cfg.reasoning_effort or "", 3, 3600, frozen, cfg.profile_id)
            return self.view(host.start_improvement(request, options, request_id=request_id), row["project_id"])

    def cancel(self, ident, task_id):
        row, manager = self.selected(ident)
        with self.applications._operation_lock(row):
            host = manager.source_controller(row["project_id"], task_id=task_id)
            self.view(host.get(task_id), row["project_id"])
            return self.view(host.cancel(task_id), row["project_id"])

    def install(self, ident, task_id):
        row, manager = self.selected(ident)
        with self.applications._operation_lock(row):
            host = manager.source_controller(row["project_id"], task_id=task_id)
            self.view(host.get(task_id), row["project_id"])
            return manager.install_delivery(row["project_id"], task_id)
