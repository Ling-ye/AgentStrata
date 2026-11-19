"""Bind an issue to the existing, limited development worker of a Studio project."""

import hashlib
import json
import re
import time
from uuid import UUID

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.core.source_snapshot import git_output, manifest_digest, source_manifest
from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
from chatcopilot.harness.gateway_adapter import task_source
from console.control import gateway_observability, project_evaluation
from console.control.application_development import Development
from console.control.application_management import Management
from studio import qq_sources


def task_host(apps, row, task_id):
    """Read only an existing project's recorded task and its original verification environment."""
    from studio import runtime

    if row["source"] != "studio":
        raise KeyError(task_id)
    project = row["project_id"]
    if row["type"] == "qq_official":
        host = apps.manager().source_controller(project, task_id=task_id)
        owner = "qq-app-" + project
    else:
        studio = apps.studio()
        with studio.db.connect() as db:
            record = db.execute(
                "SELECT m.version,p.bot FROM messages m JOIN projects p ON p.id=m.project "
                "WHERE p.id=? AND m.task=?",
                (project, task_id),
            ).fetchone()
        if not record or not re.fullmatch(r"initial|[0-9a-f]{64}", record["version"]):
            raise KeyError(task_id)
        state = studio.state_dir / "projects" / project
        if (state / record["version"]).resolve() != state / record["version"]:
            raise project_evaluation.ProjectIssue("当前开发任务的原始源码位置已变化")
        host = runtime.controller(
            state / record["version"], state, task_id=task_id, **studio.environment()
        )
        owner = record["bot"]
    if host.summary(task_id).get("source", {}).get("bot_id") != owner:
        raise KeyError(task_id)
    return host


def source(apps, row, kind, ident):
    if kind == "evaluation":
        client, bot = project_evaluation.connection(apps, row, verify_source=True)
        evaluator = ServiceEvaluator(client)
        preview = evaluator.load_instance(ident)
        if preview.get("bot_id") != bot:
            raise KeyError(ident)
        if preview["blockers"]:
            raise project_evaluation.ProjectIssue("；".join(preview["blockers"]))
        value = evaluator.source_instance(ident)
        repo, _, _ = project_evaluation.target(apps, row)
        revision = (
            client.get(value["evaluation_id"], include_result=False).get("source_revision") or {}
        )
        if (
            revision.get("commit") != git_output(repo, "rev-parse", "HEAD")
            or revision.get("dirty") is not False
        ):
            raise project_evaluation.ProjectIssue(
                "原测评与当前源码版本无法绑定，请在本应用重新测评"
            )
        trial = next(t for t in value["trials"] if t.get("trial_id") == value["trial_id"])
        facts = {
            k: value[k]
            for k in (
                "evaluation_id",
                "case_instance_id",
                "suite_id",
                "case_id",
                "case_ref",
                "trial_id",
                "attempt",
            )
        }
        facts.update(
            outcome=trial["outcome"],
            failure=value["failure_signature"],
            definition=value["case_definition"],
            trial=trial,
            conditions=value["conditions"],
            trace=trial.get("execution", {}).get("metadata", {}).get("trace"),
        )
    else:
        management = Management(apps)
        if row["source"] != "studio" or row["type"] != "qq_official":
            raise project_evaluation.ProjectIssue(
                "请先导入与此运行端匹配的公开开发源码；保留原运行记录供核对"
            )
        instance = management.instance(row["id"])
        value = task_source(gateway_observability.reader(instance), instance.instance_id, ident)
        selected = qq_sources.resolve(apps.manager(), row["project_id"])
        version = (value["evidence"].get("configuration") or {}).get("application_version")
        if (
            not selected
            or selected[1]["kind"] != "installed"
            or version != {"bot_id": instance.instance_id, "source_digest": selected[1]["digest"]}
        ):
            raise project_evaluation.ProjectIssue(
                "请先在版本与交付中安装已验收源码，启动该版本并重新采集业务任务"
            )
        if value.get("blockers"):
            raise project_evaluation.ProjectIssue("；".join(value["blockers"]))
        repo = selected[0]
        facts = {
            "run_id": ident,
            "original_input": value.get("original_input", ""),
            "revision": value["revision"],
            "failure": value["failure_signature"],
            "warnings": value.get("warnings", []),
            "evidence": value["evidence"],
        }
    receipt = {
        "kind": kind,
        "id": ident,
        "application": row["id"],
        "source_digest": manifest_digest(source_manifest(repo)),
        **facts,
    }
    return {**receipt, "digest": hashlib.sha256(json_text(receipt).encode()).hexdigest()}


def start(apps, row, request_id, kind, source_id, digest, feedback):
    from chatcopilot.harness.models import HarnessError

    if row["source"] != "studio":
        raise PermissionError("此应用没有绑定独立开发项目")
    project, request_id = row["project_id"], UUID(request_id).hex
    studio = apps.studio()
    with studio.db.connect() as db:
        old = db.execute("SELECT * FROM studio_repair_sources WHERE id=?", (request_id,)).fetchone()
    if old:
        if (old["project"], old["kind"], old["source_id"], old["digest"], old["feedback"]) != (
            project,
            kind,
            source_id,
            digest,
            feedback,
        ):
            raise project_evaluation.ProjectIssue("同一修复请求不能更换来源或预期")
        receipt = json.loads(old["receipt"])
    else:
        receipt = source(apps, row, kind, source_id)
        if receipt["digest"] != digest:
            raise project_evaluation.ProjectIssue("来源证据已变化，请重新核对")
    goal = "修复本应用在 " + source_id + " 中记录的问题。按冻结的 repair_origin 核对原始证据。"
    if feedback:
        goal += "\n用户补充\n" + feedback
    with studio.db.connect(write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO studio_repair_sources VALUES (?,?,?,?,?,?,?)",
            (request_id, project, kind, source_id, digest, feedback, json_text(receipt)),
        )
        saved = db.execute(
            "SELECT project,kind,source_id,digest,feedback FROM studio_repair_sources WHERE id=?",
            (request_id,),
        ).fetchone()
        if tuple(saved) != (project, kind, source_id, digest, feedback):
            raise project_evaluation.ProjectIssue("修复请求标识已被使用")
        if row["type"] != "qq_official":
            original = db.execute(
                "SELECT project,goal,task FROM messages WHERE id=?", (request_id,)
            ).fetchone()
            if original:
                if (original["project"], original["goal"]) != (project, goal):
                    raise project_evaluation.ProjectIssue("请求已用于其他操作")
                return {"message_id": request_id, "task_id": original["task"]}
            version = db.execute("SELECT version FROM projects WHERE id=?", (project,)).fetchone()[
                0
            ]
            if not version:
                raise project_evaluation.ProjectIssue("请先确认应用首版方案")
            db.execute(
                "INSERT INTO messages(id,project,text,state,goal,version,updated) VALUES (?,?,?,'starting',?,?,?)",
                (request_id, project, goal, goal, version, time.time()),
            )
            return {"message_id": request_id}
    try:
        return Development(apps).start(row["id"], request_id, goal, repair=receipt)
    except HarnessError as exc:
        raise project_evaluation.ProjectIssue(
            "本应用修复尚未就绪，请核对开发账号、源码与测评用例"
        ) from exc
