"""Persist a new application's first proposal and its explicit development authorization."""

import json
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from chatcopilot.core.private_sqlite import private_lock

TABLE = """CREATE TABLE IF NOT EXISTS first_plans (
    project TEXT PRIMARY KEY REFERENCES projects(id), requested_name TEXT NOT NULL, icon TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1, target TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
    acceptance TEXT NOT NULL DEFAULT '', approved INTEGER NOT NULL DEFAULT 0, delivery TEXT)"""
TARGETS = {"web", "desktop", "source", "qq_official"}


class DeliveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    platforms: list[Literal["windows-x64", "linux-x64"]] = Field(min_length=1, max_length=2)
    entry: str = Field(min_length=1, max_length=4000)
    services: str = Field(min_length=1, max_length=4000)
    bindings: str = Field(min_length=1, max_length=4000)


def checked_delivery(target, value):
    from studio.installer import platforms

    try:
        plan = DeliveryPlan.model_validate(value)
    except ValidationError:
        raise ValueError("请完整填写交付平台、启动入口、所需服务和接收方首次配置。") from None
    if len(set(plan.platforms)) != len(plan.platforms) or not set(plan.platforms) <= set(platforms(target)):
        raise ValueError("交付平台与应用形态不匹配")
    return plan.model_dump_json()


def ensure_schema(db):
    db.execute(TABLE)
    if "delivery" not in {row[1] for row in db.execute("PRAGMA table_info(first_plans)")}:
        db.execute("ALTER TABLE first_plans ADD COLUMN delivery TEXT")


def create(studio, project, name, icon, requirement):
    requirement = requirement.strip()
    if not requirement:
        raise ValueError("请填写应用需求")
    studio.directory(project)
    with (
        private_lock(studio.state_dir / ("project-create-" + project + ".lock"), timeout=10),
        studio.db.connect(write=True) as db,
    ):
        old = db.execute(
            "SELECT f.requested_name,f.icon,m.text FROM first_plans f JOIN messages m ON m.id=f.project "
            "WHERE f.project=?",
            (project,),
        ).fetchone()
        if old:
            if tuple(old) != (name, icon, requirement):
                raise ValueError("同一创建请求不能更换需求、名称或图标")
            return
        if (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_imports'").fetchone()
            and db.execute("SELECT 1 FROM studio_imports WHERE id=?", (project,)).fetchone()
        ):
            raise ValueError("此编号属于已有导入请求")
        now = time.time()
        db.execute(
            "INSERT INTO projects(id,name,bot,version,created) VALUES (?,?,?,'',?)",
            (project, name or "新应用", "app-" + project, now),
        )
        db.execute(
            "INSERT INTO first_plans(project,requested_name,icon) VALUES (?,?,?)",
            (project, name, icon),
        )
        db.execute(
            "INSERT INTO messages(id,project,text,state,version,updated) VALUES (?,?,?,'planning','',?)",
            (project, project, requirement, now),
        )


def read(studio, project):
    with studio.db.connect() as db:
        row = db.execute(
            "SELECT p.name,f.*,m.text AS requirement,m.reply,m.state FROM first_plans f "
            "JOIN projects p ON p.id=f.project JOIN messages m ON m.id=f.project WHERE f.project=?",
            (project,),
        ).fetchone()
        if row is None:
            raise KeyError("此应用没有首版方案")
        return {**row, "delivery": json.loads(row["delivery"]) if row["delivery"] else None}


def review(studio, project, revision, *, action, name="", scope="", acceptance="", target="", delivery=None):
    with studio.db.connect(write=True) as db:
        row = db.execute(
            "SELECT f.*,m.state FROM first_plans f JOIN messages m ON m.id=f.project "
            "WHERE f.project=?",
            (project,),
        ).fetchone()
        if row is None:
            raise KeyError("首版方案不存在")
        if revision != row["revision"]:
            raise ValueError("方案已更新，请读取当前版本")
        if action == "confirm" and row["approved"] and row["state"] != "initialization_failed":
            return
        if action == "retry" and row["state"] == "plan_failed":
            db.execute("UPDATE first_plans SET revision=revision+1 WHERE project=?", (project,))
            state = "planning"
        elif (
            action == "edit"
            and row["state"] in {"plan_review", "plan_failed"}
            and not row["approved"]
        ):
            if target not in TARGETS or not all(v.strip() for v in (name, scope, acceptance)):
                raise ValueError("请填写名称、范围、应用形态和验收条件")
            db.execute(
                "UPDATE first_plans SET revision=revision+1,target=?,scope=?,acceptance=?,delivery=? WHERE project=?",
                (target, scope.strip(), acceptance.strip(), checked_delivery(target, delivery), project),
            )
            db.execute("UPDATE projects SET name=? WHERE id=?", (name.strip(), project))
            state = "plan_review"
        elif action == "confirm" and row["state"] in {"plan_review", "initialization_failed"}:
            if not row["delivery"] and not row["approved"]:
                raise ValueError("请先调整方案并核对交付信息")
            db.execute("UPDATE first_plans SET approved=1 WHERE project=?", (project,))
            state = "initializing"
        else:
            raise ValueError("当前方案状态不支持此操作")
        db.execute(
            "UPDATE messages SET state=?,reply='',updated=? WHERE id=?",
            (state, time.time(), project),
        )


def generate(studio, app, message):
    import asyncio
    from studio import runtime

    project = app["id"]
    plan = read(studio, project)
    context = json.dumps(
        {"operation": "first_plan", "name": plan["requested_name"], "requirement": message["text"]},
        ensure_ascii=False,
    )
    decision = asyncio.run(
        runtime.discuss(
            studio.state_dir,
            project,
            project + "-plan-" + str(plan["revision"]),
            context,
            planning=True,
        )
    )
    if (
        decision.get("action") != "plan"
        or not isinstance(decision.get("target"), str)
        or decision["target"] not in TARGETS
        or not all(
            isinstance(decision.get(k), str) and decision[k].strip()
            for k in ("name", "scope", "acceptance")
        )
        or len(decision["name"].strip()) > 100
        or any(len(decision[k].strip()) > 16000 for k in ("scope", "acceptance"))
    ):
        raise ValueError("未取得完整的首版方案。请重新生成，或手动修改方案。")
    delivery = checked_delivery(decision["target"], decision.get("delivery"))
    with studio.db.connect(write=True) as db:
        db.execute(
            "UPDATE first_plans SET scope=?,acceptance=?,target=?,delivery=? WHERE project=?",
            (
                decision["scope"].strip(),
                decision["acceptance"].strip(),
                decision["target"],
                delivery,
                project,
            ),
        )
        db.execute(
            "UPDATE projects SET name=? WHERE id=?",
            (plan["requested_name"] or decision["name"], project),
        )
        db.execute(
            "UPDATE messages SET state='plan_review',updated=? WHERE id=?", (time.time(), project)
        )


def initialize(studio, app, work: Path):
    from studio import projects
    from studio.applications import ApplicationManager

    project = app["id"]
    plan = read(studio, project)
    if not plan["approved"]:
        raise ValueError("需要先确认首版方案")
    goal = f"用户需求\n{plan['requirement']}\n\n确认的首版范围\n{plan['scope']}\n\n验收条件\n{plan['acceptance']}"
    if plan["delivery"]:
        delivery = plan["delivery"]
        goal += (f"\n\n确认的交付平台：{', '.join(delivery['platforms'])}\n启动入口：{delivery['entry']}"
                 f"\n所需服务：{delivery['services']}\n接收方首次配置：{delivery['bindings']}")
    bot = app["bot"]
    if plan["target"] == "qq_official":
        manager = ApplicationManager(
            studio.state_dir, studio.framework, model_path=studio.model_path
        )
        manager.configure(
            project, {"name": plan["name"], "instructions": plan["scope"]}, proposal_id=project
        )
        manager.prepare_source(project)
        bot, version, state = "qq-official-" + project, "", "applied"
        reply = "已保存 QQ 应用配置并准备公开源码。可在「下载与部署」生成安装包，在应用配置中连接 QQ 官方机器人。"
    else:
        projects.new_project(studio.framework, work, bot, plan["name"], target=plan["target"],
                             platforms=plan["delivery"]["platforms"] if plan["delivery"] else None)
        if plan["target"] == "web":
            goal += (
                f"\n交付浏览器应用，在 integrations/{bot}/index.html 及相邻静态资源实现已确认范围。"
            )
        version, state, reply = "initial", "starting", "首版方案已确认，正在准备开发。"
    with studio.db.connect(write=True) as db:
        db.execute("UPDATE projects SET version=?,bot=? WHERE id=?", (version, bot, project))
        db.execute(
            "UPDATE messages SET state=?,version=?,goal=?,reply=?,updated=? WHERE id=?",
            (state, version, goal, reply, time.time(), project),
        )
