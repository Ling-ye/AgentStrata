"""Durable project conversations. Execution and verification remain owned by Harness."""
from __future__ import annotations

import asyncio
from pathlib import Path
import json
import sqlite3
import time
import tempfile
import uuid

from chatcopilot.core.private_sqlite import PrivateDatabase, private_directory, private_lock
from chatcopilot.harness.models import TERMINAL, HarnessError
from studio import delivery, first_plan, projects, runtime

_SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, bot TEXT NOT NULL, version TEXT NOT NULL, assets TEXT, created REAL NOT NULL);
CREATE TABLE messages(id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), text TEXT NOT NULL,
 state TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', goal TEXT, task TEXT, version TEXT, build TEXT, launched REAL, updated REAL NOT NULL);
CREATE INDEX conversation ON messages(project);
CREATE UNIQUE INDEX one_operation ON messages(project) WHERE state IN ('thinking','starting','developing','verifying','packaging');
"""


class Studio:
    def __init__(self, home, framework, *, model_path=None):
        self.state_dir = private_directory(home)
        self.framework = framework
        self.model_path = Path(model_path) if model_path else None
        self.db = PrivateDatabase(home / "studio.sqlite", _SCHEMA)
        with self.db.connect(write=True) as db:
            first_plan.ensure_schema(db)
            db.execute("CREATE TABLE IF NOT EXISTS studio_repair_sources (id TEXT PRIMARY KEY, "
                       "project TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL, source_id TEXT NOT NULL, "
                       "digest TEXT NOT NULL, feedback TEXT NOT NULL, receipt TEXT NOT NULL)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_console_operation ON messages(project) "
                       "WHERE state IN ('thinking','console-thinking','starting','developing','verifying','packaging')")

    def environment(self):
        if self.model_path is None:
            return {}
        return {"environment": {"AGENTSTRATA_LLM_CONFIG": str(self.model_path)}}

    def create(self, project_id, *, live=False, application_type="desktop", selection=None):
        self.directory(project_id)
        with private_lock(self.state_dir / ("project-create-" + project_id + ".lock"), timeout=1):
            return self._create(project_id, live=live, application_type=application_type, selection=selection)

    def _create(self, project_id, *, live=False, application_type="desktop", selection=None):
        with self.db.connect() as db:
            if db.execute("SELECT 1 FROM first_plans WHERE project=?", (project_id,)).fetchone():
                raise ValueError("此请求已绑定首版方案，请进入原应用")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_imports'").fetchone():
                reserved = db.execute("SELECT 1 FROM studio_imports WHERE id=?", (project_id,)).fetchone()
                if reserved and not (live and selection is not None):
                    raise ValueError("此请求标识属于桌面伙伴导入，请使用原导入请求")
        if application_type not in {"desktop", "qq_official"} or (live and application_type != "desktop"):
            raise ValueError("不支持的应用类型")
        with self.db.connect() as db:
            existing = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if existing:
            if ((existing["bot"] == "live2d-desktop") != live
                    or existing["bot"].startswith("qq-official-") != (application_type == "qq_official")):
                raise ValueError("已存在的会话属于另一种应用")
            return project_id
        bot, title, version, assets = "app-" + project_id, "新应用", "", None
        if application_type == "qq_official":
            bot, title = "qq-official-" + project_id, "AgentStrata Bot"
        if live:
            root, payload = selection if selection is not None else projects.select_live2d()
            projects.import_live2d(self.directory(project_id), root, payload)
            bot, title, version, assets = "live2d-desktop", "AgentStrata Companion", "initial", str(payload)
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
            rows = db.execute("SELECT p.id,p.name,p.bot,f.icon,f.target,m.state AS plan_state,m.updated "
                              "FROM projects p LEFT JOIN first_plans f ON f.project=p.id "
                              "LEFT JOIN messages m ON m.id=(SELECT x.id FROM messages x WHERE x.project=f.project "
                              "ORDER BY x.rowid DESC LIMIT 1) ORDER BY p.created DESC")
            return [{**dict(row), "application_type": "draft" if row["plan_state"] in {
                     "planning", "plan_failed", "plan_review", "initializing", "initialization_failed"} else
                     "qq_official" if row["bot"].startswith("qq-official-") else
                     row["target"] if row["target"] in {"web", "source"} else "desktop"} for row in rows]

    def conversation(self, project_id):
        with self.db.connect() as db:
            project = db.execute("SELECT version FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise KeyError("会话不存在")
            rows = [dict(row) for row in db.execute(
                "SELECT id,text,state,reply,task,build,launched,version,goal FROM messages WHERE project=? ORDER BY rowid", (project_id,))]
        for row in rows:
            if self.recovery_blocked(project_id, row, project["version"]):
                row.update(read_only=True, retryable=False, blocks_development=True,
                    recovery_message="此未完成记录的原任务、当前版本或冻结验证证据无法核对。记录保持只读；请先核对原版本和任务证据，当前应用暂不开始新的开发。")
            elif row["state"] == "failed":
                row["retryable"] = row["version"] == project["version"] and self.retryable(project_id, row)
                row["read_only"] = not row["retryable"]
                if row["read_only"]:
                    row["recovery_message"] = "原任务不属于当前应用版本，或独立验证证据缺失、已变化；原记录只读，请提出新的开发请求。"
            row.pop("version")
            proposal = row.pop("goal")
            if row["state"] == "configure" and proposal:
                row["proposal"] = json.loads(proposal)
        return rows

    def recovery_blocked(self, project_id, row, current_version):
        if row["state"] not in {"thinking", "starting", "developing", "verifying", "packaging"}:
            return False
        if row["version"] != current_version:
            return True
        if row["state"] in {"developing", "verifying", "packaging"} and not row["task"]:
            return True
        return bool(row["task"]) and not self.retryable(project_id, row)

    def retryable(self, project_id, row, *, fresh=False):
        if not row["task"]:
            return True
        try:
            if uuid.UUID(project_id).hex != project_id:
                return False
            work = self.state_dir / "projects" / project_id
            database = work / "harness" / "harness.sqlite3"
            if database.is_symlink() or not database.is_file():
                return False
            if fresh:
                runtime.verify_task_verification_runtime(work / row["version"], work, row["task"], fresh=True)
            else:
                digest = runtime.task_verification_id(work / row["version"], work, row["task"])
                if not digest:
                    return False
                frozen, checked = runtime.task_verification_runtime(work / row["version"], work, row["task"])
                if digest != checked or not frozen.is_dir():
                    return False
            return True
        except (ValueError, KeyError, OSError, TypeError, HarnessError, sqlite3.Error):
            if fresh:
                for check in (runtime.task_verification_id, runtime.task_verification_runtime):
                    clear = getattr(check, "cache_clear", None)
                    if clear is not None:
                        clear()
            return False

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
            if db.execute("SELECT 1 FROM first_plans f JOIN messages m ON m.id=f.project "
                          "WHERE f.project=? AND (f.approved=0 OR m.state IN ('initializing','initialization_failed'))",
                          (project_id,)).fetchone():
                raise ValueError("请先完成当前应用的首版方案确认")
            db.execute("INSERT INTO messages(id,project,text,state,version,updated) VALUES (?,?,?,'thinking',?,?)",
                       (message_id, project_id, text, project["version"], time.time()))

    def retry(self, project_id, message_id):
        with self.db.connect(write=True) as db:
            row = db.execute("SELECT m.*,p.version AS current FROM messages m JOIN projects p ON p.id=m.project "
                             "WHERE m.id=? AND p.id=?", (message_id, project_id)).fetchone()
            if not row or row["state"] != "failed" or row["version"] != row["current"]:
                raise ValueError("原任务无法在当前版本重试，请提出新的改进")
            if not self.retryable(project_id, row, fresh=True):
                raise ValueError("旧任务及证据保持只读，请发送新的开发请求")
            db.execute("UPDATE messages SET state=?,reply='' WHERE id=?",
                       ("starting" if row["task"] or row["goal"] else "thinking", message_id))

    def pending(self):
        with self.db.connect() as db:
            rows = list(db.execute("SELECT m.*,p.version AS current FROM messages m JOIN projects p ON p.id=m.project "
                    "WHERE m.state IN ('planning','initializing','thinking','starting','developing','verifying','packaging') ORDER BY m.rowid"))
        return [row["id"] for row in rows if not self.recovery_blocked(row["project"], row, row["current"])]

    def update(self, msg, state, reply="", **values):
        values.update(state=state, reply=reply, updated=time.time())
        with self.db.connect(write=True) as db:
            db.execute("UPDATE messages SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                       (*values.values(), msg))

    def step(self, msg):
        with self.db.connect() as db:
            row = dict(db.execute("SELECT * FROM messages WHERE id=?", (msg,)).fetchone())
            app = dict(db.execute("SELECT p.*, f.target AS planned_target, f.delivery AS planned_delivery "
                                  "FROM projects p LEFT JOIN first_plans f ON f.project=p.id WHERE p.id=?",
                                  (row["project"],)).fetchone())
        if self.recovery_blocked(app["id"], row, app["version"]):
            return
        work = self.directory(app["id"])
        state = row["state"]
        try:
            if state == "planning":
                first_plan.generate(self, app, row)
                return
            if state == "initializing":
                first_plan.initialize(self, app, work)
                return
            if state == "thinking":
                standard = app["bot"].startswith("qq-official-")
                definition = {}
                if standard:
                    from studio.applications import ApplicationManager
                    saved = ApplicationManager(self.state_dir, self.framework).get(app["id"])
                    definition = {key: saved.get(key, "") for key in ("name", "instructions")}
                context = json.dumps({"saved_definition": definition, "application_type": "qq_official" if standard else app["planned_target"] or "desktop",
                                      "delivery": json.loads(app["planned_delivery"]) if app["planned_delivery"] else None,
                                      "current_application": app["name"], "already_bound": bool(app["version"]),
                                      "message": row["text"]}, ensure_ascii=False)
                decision = asyncio.run(runtime.discuss(self.state_dir, app["id"], msg, context))
                if decision["action"] == "configure" and not standard and not app["version"] and self.model_path is None:
                    with self.db.connect(write=True) as db:
                        db.execute("UPDATE projects SET bot=? WHERE id=?", ("qq-official-" + app["id"], app["id"]))
                    standard = True
                if standard:
                    if decision["action"] == "configure":
                        proposal = {"name": str(decision.get("name") or app["name"]).strip()[:100],
                                    "instructions": str(decision.get("instructions") or "").strip()[:8000]}
                        if not proposal["instructions"]:
                            self.update(msg, "discuss", "请描述机器人的用途和回复要求。")
                            return
                        self.update(msg, "configure", decision["reply"], goal=json.dumps(proposal, ensure_ascii=False))
                    elif decision["action"] == "develop":
                        self.update(msg, "discuss", "此请求需要新增代码。当前 QQ 工作台先支持用途配置和运行；代码改进请使用高级开发入口，不能把尚未实现的能力当作配置启用。")
                    else:
                        self.update(msg, decision["action"], decision["reply"])
                    return
                if decision["action"] == "configure":
                    self.update(msg, "discuss", "请先创建 QQ 官方机器人应用，再配置渠道。")
                    return
                if decision["action"] != "develop":
                    self.update(msg, decision["action"], decision["reply"])
                    return
                goal = row["text"]
                if app["planned_delivery"]:
                    goal += "\n已确认的交付方案\n" + app["planned_delivery"]
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
                    host = runtime.controller(repo, work, start_eval=True, task_id=row["task"], **self.environment())
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
                    origin = db.execute("SELECT receipt FROM studio_repair_sources WHERE id=? AND project=?",
                                        (msg, app["id"])).fetchone()
                task = runtime.start_task(repo, work, app["bot"], row["goal"], msg,
                                          **self.environment(), **({"predecessor_task_id": previous["task"]} if previous else {}),
                                          **({"repair_origin": json.loads(origin["receipt"])} if origin else {}))
                self.update(msg, "developing", row["reply"], task=task["task_id"])
                return
            host = runtime.controller(repo, work, task_id=row["task"], **self.environment())
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
                self.update(msg, "packaging", "改动已通过开发验收，正在生成应用产物。")
            else:
                self.finish(row, app, repo, work, host, task)
        except Exception as exc:
            reason = str(exc)
            if isinstance(exc, FileNotFoundError) and exc.filename == str(self.state_dir / "guide.json"):
                reason = "开发助手尚未启动，请在设置里连接模型。"
            if getattr(exc, "code", "") == "verification_missing":
                reason = "应用验证环境尚未就绪。请检查聊天和开发模型连接，然后重试此请求。"
            reason = reason or type(exc).__name__
            self.update(msg, {"planning": "plan_failed", "initializing": "initialization_failed"}.get(state, "failed"),
                        ("应用打包失败。" if state == "packaging" else "") + reason)

    def finish(self, row, app, repo, work, host, task):
        from chatcopilot.harness.activation_runtime import release_source
        with self.db.connect() as db:
            planned = db.execute("SELECT target,delivery FROM first_plans WHERE project=? AND target IN ('web','source') AND approved=1", (app["id"],)).fetchone()
        with host.lifecycle.operation(row["task"]):
            release = release_source(host, row["task"])
            final = work / "builds" / row["id"]
            receipt_file = final / "build-receipt.json"
            existing = json.loads(receipt_file.read_text()) if final.is_dir() else {}
            old_archive = bool(existing.get("archive_sha256") and not existing.get("installers"))
            if old_archive and (existing.get("accepted_source_digest") != release.receipt["candidate_digest"]
                                or delivery.digest(final / "application.zip") != existing["archive_sha256"]):
                raise ValueError("已有归档与本次候选不匹配")
            if not existing or old_archive:
                with tempfile.TemporaryDirectory(dir=private_directory(final.parent)) as tmp:
                    pending = Path(tmp) / "output"
                    ident = app["bot"] if not app["assets"] else "live2d-trial-" + app["id"]
                    # Versions are side-by-side even when an older trial is installed.
                    ident += "-" + release.receipt["candidate_digest"][:10]
                    from studio import installer
                    kind = planned["target"] if planned else "companion" if app["assets"] else "desktop"
                    platforms = json.loads(planned["delivery"])["platforms"] if planned and planned["delivery"] else None
                    installer.build(release.candidate, pending, bot=app["bot"], name=app["name"], kind=kind,
                                    origin={"kind": "candidate", "revision": release.receipt["candidate_digest"], "task": row["task"]},
                                    manifest=release.candidate_manifest, targets=platforms, release=release,
                                    assets=Path(app["assets"]) if app["assets"] else None, install_id=ident)
                    if old_archive:
                        # A restart can find the earlier ZIP after packaging but before marking the message ready.
                        # Publish the new receipt last and retain the verified archive bytes.
                        for file in pending.iterdir():
                            if file.name != "build-receipt.json":
                                file.replace(final / file.name)
                        (pending / "build-receipt.json").replace(receipt_file)
                    else:
                        pending.rename(final)
            receipt = json.loads((final / "build-receipt.json").read_text())
            from studio.installer import FILES
            expected = receipt.get("installers") or {"windows-x64": {"sha256": receipt.get("installer_sha256")}}
            if (receipt["accepted_source_digest"] != release.receipt["candidate_digest"] or
                    any(delivery.digest(final / FILES[platform]) != info["sha256"] for platform, info in expected.items())):
                raise ValueError("安装包与本次候选不匹配")
            version = receipt["accepted_source_digest"]
            projects.source_tree(work, version, release.candidate, release.candidate_manifest)
        verified = "\n".join(item["text"] for item in task["acceptance"]["items"] if item.get("required"))
        method = "在「下载与部署」选择目标系统，运行应用安装包，再尝试以上操作。"
        reply = "已验证的改动\n" + verified + "\n\n试用方法\n" + method
        with self.db.connect(write=True) as db:
            db.execute("UPDATE projects SET version=? WHERE id=?", (version, app["id"]))
            db.execute("UPDATE messages SET state='ready',reply=?,build=?,updated=? WHERE id=?",
                       (reply, row["id"], time.time(), row["id"]))

    def artifact_reference(self, project_id, message_id, platform=None):
        """Resolve an application-bound receipt; callers must verify the bytes they use."""
        with self.db.connect() as db:
            row = db.execute("SELECT 1 FROM messages WHERE project=? AND id=? AND build=id AND state='ready'",
                             (project_id, message_id)).fetchone()
            if not row:
                raise KeyError("安装包尚未就绪")
        path = self.directory(project_id) / "builds" / message_id
        receipt = json.loads((path / "build-receipt.json").read_text())
        filename, digest_key = ("application.zip", "archive_sha256") if "archive_sha256" in receipt else ("application.exe", "installer_sha256")
        if "installers" in receipt:
            from studio.installer import FILES
            platform = platform or next(p for p in FILES if p in receipt["installers"])
            filename = FILES[platform]
            expected = receipt["installers"][platform]["sha256"]
        else:
            if platform not in {None, "windows-x64"}:
                raise KeyError("此构建没有该平台的安装包")
            expected = receipt[digest_key]
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise ValueError("安装包摘要记录无效")
        return path / filename, expected

    def artifact(self, project_id, message_id):
        path, expected = self.artifact_reference(project_id, message_id)
        if delivery.digest(path) != expected:
            raise ValueError("安装包摘要不一致")
        return path
