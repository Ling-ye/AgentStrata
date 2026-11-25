"""Unified application views; sources retain ownership of runtime and configuration."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import json
import time
import threading
from urllib.parse import quote
from uuid import UUID
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_file, private_lock

from console.control.discovery import discover_observable_instances, repo_root
from console.control import operations
from studio.applications import ApplicationManager
from studio.workspace import Studio


class Applications:
    def __init__(self, root=None, home=None, *, model_path=None, discover=None):
        self.root = Path(root or repo_root())
        self.state_dir = Path(home or os.environ.get("AGENTSTRATA_STUDIO_HOME", Path.home() / ".local/share/agentstrata/studio"))
        self.model_path = model_path or self.state_dir / "models.json"
        self._archive_path = self.state_dir / "application-archive.json"
        self._discover = discover or (lambda: discover_observable_instances(self.root))
        self._studio = None
        self._guide = None
        self._lock = threading.Lock()

    def studio(self):
        if self._studio is None:
            self._studio = Studio(self.state_dir, self.root, model_path=self.model_path)
        return self._studio

    def manager(self):
        return ApplicationManager(self.state_dir, self.root, model_path=self.model_path)

    def _projects(self):
        # Opening a Console read must not create a Studio installation.
        if not (self.state_dir / "studio.sqlite").is_file():
            return []
        return self.studio().projects()

    @staticmethod
    def _view(ident, name, kind, source, instance=None, project=None, **capabilities):
        return {"id": ident, "name": name, "type": kind, "source": source,
                "instance_id": instance, "project_id": project,
                "capabilities": {"configure": False, "control": False, "chat": False,
                                 "improve": False, "records": False, "settings": False, "observation": False, **capabilities},
                "links": {}, "state": {"process": "unknown", "channel": "unknown", "end_to_end": "unverified"}}

    def catalog(self):
        rows, paths = [], set()
        projects = self._projects()
        # Explicit Studio-owned generated specs must not acquire repository lifecycle actions.
        for p in projects:
            if p["application_type"] == "qq_official":
                paths.add(str((self.state_dir / "applications" / p["id"] / "bot.yaml").resolve()))
        for inst in self._discover():
            path = str(Path(inst.bot_spec).resolve())
            if Path(path) == (self.root / "bots/studio-guide/bot.yaml").resolve():
                continue
            if path in paths:
                continue
            paths.add(path)
            source = "plugin" if inst.read_only else "repository"
            ident = ("plugin:" if inst.read_only else "repo:") + inst.instance_id
            kind = {"qq": "qq_personal", "qq_personal": "qq_personal", "weixin": "weixin"}.get(inst.platform, "agent")
            if inst.origin.get("plugin_id") == "open-llm-vtuber" or Path(path) == self.root / "bots/live2d-desktop/bot.yaml":
                kind = "companion"
            elif not inst.platform:
                from console.control.yaml_io import load_yaml_mapping_or_empty
                deploy = load_yaml_mapping_or_empty(Path(path)).get("deploy")
                if isinstance(deploy, dict) and deploy.get("target") == "windows":
                    kind = "desktop"
            row = self._view(ident, inst.display_name or inst.instance_id, kind, source,
                             instance=inst.instance_id, control=not inst.read_only,
                             local_deploy=not inst.read_only and bool(inst.wsl_home) and kind not in {"desktop", "companion"},
                             records=True, observation=True, settings=not inst.read_only and inst.runtime_kind == "gateway")
            q = quote(inst.instance_id, safe="")
            row["links"] = {"configuration": "#bots?instance=" + q + "&tab=configuration",
                            "records": "#bots?instance=" + q + "&tab=tasks"}
            if not inst.read_only:
                row["links"].update(eval="#evals?application=" + q, harness="#harness?application=" + q)
            row["message"] = "组件来源应用保持只读；请在桌面客户端打开。" if inst.read_only else "运行与配置由原实例管理器负责。"
            if inst.runtime_kind == "gateway":
                try:
                    from console.control.gateway_observability import reader
                    observed = reader(inst)
                    if observed.database.exists():
                        meta = observed.meta("loaded_configuration") or {}
                        status = observed.meta("runtime_status") or {}
                        stopped = (status.get("state") == "stopped" and meta.get("generation") is not None
                                   and status.get("generation") == meta["generation"]
                                   and status.get("observed_at", 0) >= meta.get("observed_at", 0))
                        row["state"]["process"] = "stopped" if stopped else "online" if 0 <= time.time() - meta.get("observed_at", 0) < 15 else "unobserved"
                        from chatcopilot.gateway.observation_queries import history, RunFilter
                        latest = history(observed, RunFilter(limit=1))["runs"]
                        if latest:
                            row["last_activity"] = latest[0].get("updated_at") or latest[0].get("created_at")
                except (ValueError, OSError, RuntimeError):
                    row["state"]["process"] = "unavailable"
            rows.append(row)
        from console.control import released_applications
        managed = {p["id"] for p in projects}
        rows.extend(r for r in released_applications.applications(self) if r["project_id"] not in managed)
        for p in projects:
            standard = p["application_type"] == "qq_official"
            row = self._view("studio:" + p["id"], p["name"], "companion" if p["bot"] == "live2d-desktop" else p["application_type"],
                             "studio", project=p["id"], configure=standard, control=standard,
                             improve=True, records=True, settings=standard and
                             (self.state_dir / "applications" / p["id"] / "bot.yaml").is_file(), observation=standard and
                             (self.state_dir / "applications" / p["id"] / "runtime.env").is_file())
            if p.get("plan_state"):
                row.update(first_plan=True, plan_state=p["plan_state"], icon=p["icon"], last_activity=p["updated"])
            row["message"] = ("管理对话只生成配置提案；试聊与真实 QQ 往返尚未验证。"
                              if standard else "在此讨论需求、开发和验收，完成后下载桌面安装包；安装与运行另行确认。")
            if standard:
                row["state"]["process"] = "unobserved"
                row["state"]["channel"] = "not_checked"
            rows.append(row)
        from console.control.native_applications import NativeApplications
        for record in NativeApplications(self.state_dir).records():
            row = self._view(record["id"], record["name"], record["type"], "native", records=True, settings=True, observation=True)
            row["message"] = "从 Windows 桌宠客户端使用；后台在线时可查看记录和调整配置。"
            rows.append(row)
        return rows

    def _archived_ids(self) -> set[str]:
        try:
            private_file(self._archive_path)
            values = json.loads(self._archive_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError("应用归档记录无效")
        return set(values)

    def directory(self, *, archived: bool = False):
        archived_ids = self._archived_ids()
        return [row for row in self.catalog() if (row["id"] in archived_ids) == archived]

    def set_archived(self, ident: str, archived: bool):
        self.require(ident)
        with private_lock(self._archive_path.with_suffix(".lock"), timeout=10):
            archived_ids = self._archived_ids()
            if archived != (ident in archived_ids):
                if archived:
                    archived_ids.add(ident)
                else:
                    archived_ids.remove(ident)
                write_private(self._archive_path, json.dumps(sorted(archived_ids)) + "\n")
        return {"id": ident, "archived": archived}

    def get(self, ident):
        return next((row for row in self.catalog() if row["id"] == ident), None)

    def require(self, ident, capability=None):
        row = self.get(ident)
        if row is None:
            raise KeyError("应用不存在")
        if capability and not row["capabilities"][capability]:
            raise PermissionError("此应用尚不支持该操作")
        return row

    def detail(self, ident):
        row = self.require(ident)
        if row["source"] == "studio" and row["type"] == "qq_official":
            status = self.manager().get(row["project_id"])
            row["state"] = {"process": status.get("process_state", status["state"]),
                            "channel": status.get("channel", {}).get("state", "not_checked"),
                            "end_to_end": "unverified"}
            row["status"] = status
            from console.control.application_preview import Preview
            config, reason = Preview(self).settings(row)
            row["capabilities"]["chat"] = config is not None
            row["preview_message"] = reason
        if row["source"] == "native":
            from console.control.native_applications import NativeApplications
            try:
                NativeApplications(self.state_dir).read(ident, "identity")
                row["state"]["process"] = "online"
            except (OSError, ValueError, RuntimeError):
                row["state"]["process"] = "offline"
        return row

    def create(self, ident, kind):
        key = UUID(str(ident)).hex
        if kind not in {"qq_official", "desktop"}:
            raise ValueError("不支持的应用类型")
        self.studio().create(key, live=kind == "live2d",
                             application_type="qq_official" if kind == "qq_official" else "desktop")
        return self.detail("studio:" + key)

    def configuration(self, ident):
        row = self.require(ident, "configure")
        return self.manager().get(row["project_id"])

    def _operation_lock(self, row):
        return private_lock(self.state_dir / ("console-app-" + row["project_id"] + ".lock"), timeout=10)

    def configure(self, ident, values):
        row = self.require(ident, "configure")
        with self._operation_lock(row):
            return self._configure(row, values)

    def _configure(self, row, values, **kwargs):
        result = self.manager().configure(row["project_id"], values, **kwargs)
        with self.studio().db.connect(write=True) as db:
            db.execute("UPDATE projects SET name=? WHERE id=?", (result["name"], row["project_id"]))
        return result

    def action(self, ident, action):
        row = self.require(ident, "configure" if action == "check" else "control")
        if row["source"] == "studio":
            return getattr(self.manager(), action)(row["project_id"])
        inst = next((i for i in self._discover() if i.instance_id == row["instance_id"] and not i.read_only), None)
        if inst is None:
            raise KeyError("运行实例不存在")
        result = operations.control(inst, action)
        if not result.get("ok"):
            raise ValueError("运行操作失败，请查看高级管理中的实例诊断")
        return result

    def messages(self, ident):
        row = self.require(ident, "records")
        if row["source"] != "studio":
            raise PermissionError("此来源请使用应用记录入口")
        return self.studio().conversation(row["project_id"])

    def submit(self, ident, message, text):
        row = self.require(ident, "improve")
        key = UUID(str(message)).hex
        text = text.strip()
        if not text or len(text) > 16000:
            raise ValueError("请输入有效的应用需求")
        if row["type"] != "qq_official":
            with self._operation_lock(row):
                self.studio().submit(row["project_id"], key, text)
            return
        with self._operation_lock(row), self.studio().db.connect(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS console_requests (id TEXT PRIMARY KEY, context TEXT NOT NULL, baseline TEXT NOT NULL)")
            # Include legacy pending states: only one management mutation per application.
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_console_operation ON messages(project) "
                       "WHERE state IN ('thinking','console-thinking','starting','developing','verifying','packaging')")
            old = db.execute("SELECT project,text FROM messages WHERE id=?", (key,)).fetchone()
            if old:
                if (old["project"], old["text"]) != (row["project_id"], text):
                    raise ValueError("同一请求不能更换应用或内容")
                return
            current, baseline = self.manager().definition_snapshot(row["project_id"])
            context = json.dumps({"application_type": "qq_official", "already_bound": True,
                                  "saved_definition": {k: current.get(k, "") for k in ("name", "instructions")},
                                  "message": text}, ensure_ascii=False)
            db.execute("INSERT INTO console_requests VALUES (?,?,?)", (key, context, baseline))
            db.execute("INSERT INTO messages(id,project,text,state,version,updated) VALUES (?,?,?,'console-thinking','',?)",
                       (key, row["project_id"], text, time.time()))

    def process_message(self, ident, message):
        """Configuration proposals only; legacy development polling cannot consume these rows."""
        from chatcopilot.core.private_sqlite import private_lock, private_directory
        from studio.setup import Guide
        from studio import runtime
        studio = self.studio()
        row = self.require(ident, "improve")
        if row["type"] != "qq_official":
            return  # Durable Studio worker owns desktop development.
        with self._lock, private_lock(self.state_dir / "console-guide.lock", timeout=10):
            with studio.db.connect() as db:
                item = db.execute("SELECT state,text FROM messages WHERE id=? AND project=?",
                                  (message, row["project_id"])).fetchone()
            if item is None or item["state"] != "console-thinking":
                return
            guide_home = private_directory(self.state_dir / "console-guide")
            if self._guide is None:
                self._guide = Guide(guide_home, self.root, model_path=self.model_path)
            try:
                self._guide.start()
                with studio.db.connect() as db:
                    saved = db.execute("SELECT context FROM console_requests WHERE id=?", (message,)).fetchone()
                context = saved["context"]
                current = json.loads(context)["saved_definition"]
                async def bounded_discuss():
                    return await asyncio.wait_for(runtime.discuss(guide_home, row["project_id"], message, context), timeout=120)
                decision = asyncio.run(bounded_discuss())
                if decision["action"] == "configure":
                    proposal = {"name": str(decision.get("name") or current["name"]).strip()[:100],
                                "instructions": str(decision.get("instructions") or "").strip()[:8000]}
                    if not proposal["instructions"]:
                        studio.update(message, "discuss", "请描述应用的用途和回复要求。")
                    else:
                        studio.update(message, "configure", decision["reply"],
                                      goal=json.dumps(proposal, ensure_ascii=False))
                elif decision["action"] == "develop":
                    studio.update(message, "discuss", "这需要代码开发。当前应用配置对话不执行代码修改；请使用支持的高级开发入口。")
                else:
                    studio.update(message, "discuss", decision["reply"])
            except Exception:
                studio.update(message, "failed", "管理助手暂不可用，请检查模型连接后重新提出需求。")

    def apply(self, ident, message):
        row = self.require(ident, "configure")
        key = UUID(str(message)).hex
        with self._operation_lock(row):
            with self.studio().db.connect() as db:
                proposal = db.execute("SELECT goal,state FROM messages WHERE project=? AND id=?",
                                      (row["project_id"], key)).fetchone()
                if proposal and proposal["state"] == "applied":
                    return self.configuration(ident)
                latest = db.execute("SELECT id FROM messages WHERE project=? ORDER BY rowid DESC LIMIT 1",
                                    (row["project_id"],)).fetchone()
                if not proposal or proposal["state"] != "configure" or not latest or latest["id"] != key:
                    raise ValueError("配置提案已失效，请使用最新提案")
                baseline = db.execute("SELECT baseline FROM console_requests WHERE id=?", (key,)).fetchone()
                if not baseline:
                    raise ValueError("旧提案缺少配置版本，请重新生成")
            result = self._configure(row, json.loads(proposal["goal"]),
                                     expected_version=baseline["baseline"], proposal_id=key)
            self.studio().update(key, "applied", "配置已应用。")
            return result

    def retry(self, ident, message):
        row = self.require(ident, "improve")
        if row["type"] == "qq_official":
            raise PermissionError("请重新提出配置需求")
        with self._operation_lock(row):
            self.studio().retry(row["project_id"], UUID(str(message)).hex)
        return {"ok": True}

    async def resume(self):
        from studio.worker import StudioWorker
        async def development():
            while not (self.state_dir / "studio.sqlite").is_file():
                await asyncio.sleep(1)
            await StudioWorker(self.studio()).run()
        tasks = [asyncio.create_task(development()), asyncio.create_task(self._resume_configuration())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _resume_configuration(self):
        """Resume only this workbench's explicit configuration requests after restart."""
        while True:
            if (self.state_dir / "studio.sqlite").is_file():
                with self.studio().db.connect() as db:
                    pending = [(r["project"], r["id"]) for r in db.execute(
                        "SELECT project,id FROM messages WHERE state='console-thinking' ORDER BY rowid")]
                for project, message in pending:
                    try:
                        await asyncio.to_thread(self.process_message, "studio:" + project, message)
                    except (KeyError, PermissionError, OSError, TimeoutError):
                        # Do not interpret unknown outcome as success or execute another source.
                        continue
            await asyncio.sleep(1)

    async def close(self):
        def stop_when_idle():
            with self._lock:
                if self._guide:
                    self._guide.stop()
        await asyncio.to_thread(stop_when_idle)

