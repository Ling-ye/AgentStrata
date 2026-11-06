"""Durable project conversations. Execution and verification remain owned by Harness."""
from __future__ import annotations

import asyncio
from pathlib import Path
import json
import os
import time
import uuid

from chatcopilot.core.private_sqlite import PrivateDatabase, private_directory
from chatcopilot.harness.models import TERMINAL
from studio import delivery, projects, runtime

_SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, bot TEXT NOT NULL, version TEXT NOT NULL, assets TEXT, created REAL NOT NULL);
CREATE TABLE messages(id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), text TEXT NOT NULL,
 state TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', goal TEXT, task TEXT, version TEXT, build TEXT, launched REAL, updated REAL NOT NULL);
CREATE INDEX conversation ON messages(project);
CREATE UNIQUE INDEX one_operation ON messages(project) WHERE state IN ('thinking','starting','developing','verifying','packaging');
"""


class Studio:
    def __init__(self, home, framework):
        self.state_dir = private_directory(home)
        self.framework = framework
        self.db = PrivateDatabase(home / "studio.sqlite", _SCHEMA)

    def create(self, project_id, *, live=False):
        with self.db.connect() as db:
            existing = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if existing:
            if (existing["bot"] == "live2d-desktop") != live:
                raise ValueError("已存在的会话属于另一种应用")
            return project_id
        bot, title, version, assets = "app-" + project_id, "新应用", "", None
        if live:
            root, payload = projects.select_live2d()
            projects.import_live2d(self.directory(project_id), root, payload)
            bot, title, version, assets = "live2d-desktop", "Live2D 桌宠", "initial", str(payload)
        with self.db.connect(write=True) as db:
            db.execute("INSERT INTO projects VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                       (project_id, title, bot, version, assets, time.time()))
        return project_id

    def directory(self, project_id):
        if uuid.UUID(project_id).hex != project_id:
            raise ValueError("未知的项目")
        return private_directory(self.state_dir / "projects" / project_id)

    def projects(self):
        with self.db.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,name,bot FROM projects ORDER BY created DESC")]

    def conversation(self, project_id):
        with self.db.connect() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise KeyError("会话不存在")
            rows = [dict(row) for row in db.execute(
                "SELECT id,text,state,reply,task,build,launched,version FROM messages WHERE project=? ORDER BY rowid", (project_id,))]
        for row in rows:
            if row["state"] == "failed":
                row["retryable"] = self.retryable(project_id, row)
            row.pop("version")
        return rows

    def retryable(self, project_id, row):
        if not row["task"]:
            return True
        work = self.directory(project_id)
        return bool(runtime.task_verification_id(work / row["version"], work, row["task"]))

    def submit(self, project_id, message_id, text):
        text = text.strip()
        if not text:
            raise ValueError("请输入需求")
        with self.db.connect(write=True) as db:
            previous = db.execute("SELECT project,text FROM messages WHERE id=?", (message_id,)).fetchone()
            if previous:
                if (previous["project"], previous["text"]) != (project_id, text):
                    raise ValueError("同一请求不能更换项目或内容")
                return
            project = db.execute("SELECT version FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise KeyError("会话不存在")
            db.execute("INSERT INTO messages(id,project,text,state,version,updated) VALUES (?,?,?,'thinking',?,?)",
                       (message_id, project_id, text, project["version"], time.time()))

    def pending(self):
        with self.db.connect() as db:
            return [r[0] for r in db.execute("SELECT id FROM messages WHERE state IN "
                    "('thinking','starting','developing','verifying','packaging') ORDER BY rowid")]

    def update(self, msg, state, reply="", **values):
        values.update(state=state, reply=reply, updated=time.time())
        with self.db.connect(write=True) as db:
            db.execute("UPDATE messages SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                       (*values.values(), msg))

    def step(self, msg):
        with self.db.connect() as db:
            row = dict(db.execute("SELECT * FROM messages WHERE id=?", (msg,)).fetchone())
            app = dict(db.execute("SELECT * FROM projects WHERE id=?", (row["project"],)).fetchone())
        work = self.directory(app["id"])
        state = row["state"]
        try:
            if state == "thinking":
                context = json.dumps({"current_application": app["name"], "already_bound": bool(app["version"]),
                                      "message": row["text"]}, ensure_ascii=False)
                decision = asyncio.run(runtime.discuss(self.state_dir, app["id"], msg, context))
                if decision["action"] != "develop":
                    self.update(msg, decision["action"], decision["reply"])
                    return
                goal = row["text"]
                if decision.get("use_previous") is True:
                    with self.db.connect() as db:
                        previous = db.execute(
                            "SELECT state,text,reply FROM messages WHERE project=? AND rowid < "
                            "(SELECT rowid FROM messages WHERE id=?) ORDER BY rowid DESC LIMIT 1",
                            (app["id"], msg)).fetchone()
                    if not previous or previous["state"] != "discuss":
                        self.update(msg, "discuss", "请说说你希望具体实现哪项改动。")
                        return
                    goal += "\n上一轮讨论，仅用于解释本次引用：\n" + json.dumps(
                        {"用户": previous["text"], "助手": previous["reply"]}, ensure_ascii=False)
                name = str(decision.get("name") or app["name"]).strip()
                if not app["version"]:
                    projects.new_project(self.framework, work, app["bot"], name)
                with self.db.connect(write=True) as db:
                    if not app["version"]:
                        db.execute("UPDATE projects SET version='initial',name=? WHERE id=?", (name, app["id"]))
                    db.execute("UPDATE messages SET state='starting',reply=?,goal=?,version=?,updated=? WHERE id=?",
                               (decision["reply"], goal, app["version"] or "initial", time.time(), msg))
                return
            repo = work / row["version"]
            if state == "starting":
                if row["task"]:
                    host = runtime.controller(repo, work, start_eval=True, task_id=row["task"])
                    task = host.get(row["task"])
                    if task["status"] in TERMINAL and task["status"] != "fixed":
                        extra = {}
                        if (task.get("error_code") == "attempts_exhausted"
                                and task.get("elapsed_seconds", 0) < task["options"]["timeout_seconds"]):
                            extra["max_attempts"] = task["options"]["max_attempts"] + 1
                        host.resume(row["task"], **extra)
                    self.update(msg, "developing", "已重新核对原任务，正在继续。")
                    return
                if row["version"] != "initial":
                    from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
                    if manifest_digest(source_manifest(repo)) != row["version"]:
                        raise ValueError("此项目的源码已偏离上一次验收的版本，请重新核对来源。")
                with self.db.connect() as db:
                    previous = db.execute("SELECT task FROM messages WHERE project=? AND state='ready' "
                                          "AND build=id ORDER BY rowid DESC LIMIT 1", (app["id"],)).fetchone()
                task = runtime.start_task(repo, work, app["bot"], row["goal"], msg,
                                          **({"predecessor_task_id": previous["task"]} if previous else {}))
                self.update(msg, "developing", row["reply"], task=task["task_id"])
                return
            host = runtime.controller(repo, work, task_id=row["task"])
            task = host.get(row["task"])
            if task["status"] not in TERMINAL:
                stage = str(task.get("stage") or task["status"])
                verifying = stage.startswith("repository-verify-") or stage.partition("-")[0] in {
                    "test", "definition", "baseline", "reproduce", "verification", "verify", "regressions",
                    "repository_baseline", "confirmation", "confirm", "review", "seal", "acceptance",
                }
                self.update(msg, "verifying" if verifying else "developing",
                            "正在检查改动和已有功能。" if verifying else "开发任务正在处理这次需求。")
            elif task["status"] != "fixed" or not task.get("accepted_candidate"):
                runtime.stop_evaluation(repo, work, row["task"])
                reason = str(task.get("message") or task.get("error") or task["status"])
                self.update(msg, "failed", ("验证未完成。" if state == "verifying" else "开发未完成。") + reason)
            elif state != "packaging":
                runtime.stop_evaluation(repo, work, row["task"])
                self.update(msg, "packaging", "改动已通过开发验收，正在生成 Windows 安装包。")
            else:
                self.finish(row, app, repo, work, host, task)
        except Exception as exc:
            reason = str(exc)
            if isinstance(exc, FileNotFoundError) and exc.filename == str(self.state_dir / "guide.json"):
                reason = "开发助手尚未启动，请在设置里连接模型。"
            if getattr(exc, "code", "") == "verification_missing":
                reason = "应用验证环境尚未就绪。请检查聊天和开发模型连接，然后重试此请求。"
            reason = reason or type(exc).__name__
            self.update(msg, "failed", ("Windows 安装包生成失败。" if state == "packaging" else "") + reason)

    def finish(self, row, app, repo, work, host, task):
        from chatcopilot.harness.activation_runtime import release_source
        with host.lifecycle.operation(row["task"]):
            release = release_source(host, row["task"])
            final = work / "builds" / row["id"]
            if not final.is_dir():
                pending = final.with_name(row["id"] + "-" + uuid.uuid4().hex)
                ident = app["bot"] if not app["assets"] else "live2d-trial-" + app["id"]
                # Versions are side-by-side even when an older trial is installed.
                ident += "-" + release.receipt["candidate_digest"][:10]
                delivery.pack(repo, pending, bot_relative=release.bot_relative, release=release,
                              assets=Path(app["assets"]) if app["assets"] else None, name=app["name"],
                              install_id=ident, windows_build=os.environ.get("AGENTSTRATA_STUDIO_WINDOWS_BUILD"))
                pending.rename(final)
            receipt = json.loads((final / "build-receipt.json").read_text())
            if receipt["accepted_source_digest"] != release.receipt["candidate_digest"] or delivery.digest(final / "application.exe") != receipt["installer_sha256"]:
                raise ValueError("安装包与本次候选不匹配")
            version = receipt["accepted_source_digest"]
            projects.source_tree(work, version, release.candidate, release.candidate_manifest)
        verified = "\n".join(item["text"] for item in task["acceptance"]["items"] if item.get("required"))
        reply = "已验证的改动\n" + verified + "\n\n试用方法\n点击「试用桌面版」安装，打开应用后尝试以上操作。"
        with self.db.connect(write=True) as db:
            db.execute("UPDATE projects SET version=? WHERE id=?", (version, app["id"]))
            db.execute("UPDATE messages SET state='ready',reply=?,build=?,updated=? WHERE id=?",
                       (reply, row["id"], time.time(), row["id"]))

    def artifact(self, project_id, message_id):
        with self.db.connect() as db:
            row = db.execute("SELECT 1 FROM messages WHERE project=? AND id=? AND build=id AND state='ready'",
                             (project_id, message_id)).fetchone()
            if not row:
                raise KeyError("安装包尚未就绪")
        path = self.directory(project_id) / "builds" / message_id
        if delivery.digest(path / "application.exe") != json.loads((path / "build-receipt.json").read_text())["installer_sha256"]:
            raise ValueError("安装包摘要不一致")
        return path / "application.exe"
