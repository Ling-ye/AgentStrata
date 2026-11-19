"""Explicit QQ development, scoped to a controlled application source and service."""
from __future__ import annotations

import os
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

from chatcopilot.core.model_settings import worker_profile
from chatcopilot.core.private_sqlite import private_directory, private_file, sqlite_uri
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

    def request(self, ident, request_id):
        """Reconcile a lost acknowledgement without starting a worker or selecting a new baseline."""
        row, _ = self.selected(ident)
        with self.applications._operation_lock(row):
            return self._request(row, UUID(str(request_id)).hex)

    def _request(self, row, request_id):
        result = {"id": request_id, "state": "not_found"}
        with self.applications.studio().db.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='application_development_requests'").fetchone():
                return result
            intent = db.execute("SELECT project,goal,source FROM application_development_requests WHERE id=?", (request_id,)).fetchone()
        if intent is None:
            return result
        if intent["project"] != row["project_id"]:
            raise KeyError("请求不属于当前应用")
        if not re.fullmatch(r"[a-f0-9]{64}", intent["source"]):
            raise ValueError("原请求源码绑定无效")
        result.update(state="pending", goal=intent["goal"])
        # The original source digest, not the current configuration or browser,
        # selects this existing task index. A query must not initialize a store.
        directory = self.applications.state_dir / "applications" / row["project_id"] / "development" / intent["source"] / "harness"
        database = directory / "harness.sqlite3"
        if not database.exists() and not database.is_symlink():
            return result
        private_directory(directory, create=False)
        before = private_file(database)
        with database.open("rb") as stream:
            header = stream.read(20)
        if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise ValueError("原任务库日志尚未收尾，请保留原请求")
        with closing(sqlite3.connect(sqlite_uri(database, mode="ro"), uri=True, timeout=5)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            task = db.execute("SELECT task_id,payload FROM tasks WHERE request_key=?", (request_id,)).fetchone()
            after = private_file(database)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("原任务库身份已变化")
        if task is None:
            return result
        if not isinstance(task[1], str) or len(task[1].encode("utf-8")) > 8 * 1024 * 1024:
            raise ValueError("原任务元数据无法读取")
        value = json.loads(task[1])
        if (not isinstance(value, dict) or value.get("task_id") != task[0]
                or value.get("request_key") != request_id or not re.fullmatch(r"repair-[a-f0-9]{32}", task[0])):
            raise ValueError("原任务请求身份不匹配")
        result.update(state="submitted", task={**self.view(value, row["project_id"]), "request_id": request_id})
        return result

    def start(self, ident, request_id, goal, *, repair=None):
        row, manager = self.selected(ident)
        request_id = UUID(str(request_id)).hex
        goal = goal.strip()
        if not goal or len(goal) > 16000:
            raise ValueError("请输入具体的开发目标")
        with self.applications._operation_lock(row):
            try:
                previous = self._request(row, request_id)
            except KeyError:
                raise ValueError("同一开发请求不能更换应用") from None
            if previous["state"] != "not_found" and previous["goal"] != goal:
                raise ValueError("同一开发请求不能更换目标")
            if previous["state"] == "submitted":
                return previous["task"]
            manager._require_stopped(row["project_id"])
            state = manager.source_status(row["project_id"])
            if repair is not None and state.get("source_digest") != repair["source_digest"]:
                raise ValueError("修复来源与当前 QQ 源码不一致，请重新核对")
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
            case_ids = (repair["case_id"],) if repair and repair["kind"] == "evaluation" else ()
            suite = repair["suite_id"] if case_ids else "agentstrata-agent-tasks-v1"
            if case_ids:
                host.evaluator.application_checks = case_ids
            request = ImprovementRequest("improve", bot, goal, scope, case_ids, suite_id=suite)
            options = RepairOptions(cfg.model, cfg.reasoning_effort or "", 3, 3600, frozen, cfg.profile_id)
            task = host.start_improvement(request, options, request_id=request_id, **({"repair_origin":repair} if repair else {}))
            return {**self.view(task, row["project_id"]), "request_id": request_id}

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
