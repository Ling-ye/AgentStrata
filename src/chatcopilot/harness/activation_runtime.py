"""Bind accepted Harness evidence to the public installed-version control plane."""
from __future__ import annotations

from pathlib import Path

from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.core.source_snapshot import manifest_digest, source_manifest, verify_copy
from chatcopilot.harness.delivery_candidate import accepted
from chatcopilot.harness.models import HarnessError, PIPELINE_VERSION
from chatcopilot.plugins.activation_types import ReleaseSource
from chatcopilot.plugins.activation_service import ActivationService


def _authorized(controller, task_id):
    current = controller.store.get(task_id)
    return current["status"] == "fixed" and not current.get("delivery_cancel_requested")


def release_source(controller, task_id):
    task = controller.store.get(task_id)
    if (task.get("pipeline_version") != PIPELINE_VERSION or task["source"].get("kind") != "application"
            or not accepted(controller.store, task)):
        raise HarnessError("activation_not_accepted", "只有当前协议完整验收的应用需求候选可以启用")
    receipt = task["accepted_candidate"]
    attempt = next(row for row in controller.store.attempts(task_id) if row["number"] == receipt["attempt"])
    if (attempt.get("review", {}).get("decision") != "approved"
            or attempt["review"].get("binding") != receipt["review_binding"] or attempt.get("regressions")):
        raise HarnessError("activation_not_accepted", "候选缺少对应版本的独立审核")
    job = controller.store.root / "jobs" / task_id
    candidate = Path(attempt["snapshot_path"])
    if candidate.resolve() != candidate or not candidate.is_relative_to(job / "snapshots"):
        raise HarnessError("artifact_changed", "候选快照位置发生变化")
    before, after = task["baseline_manifest"], task["verified_manifest"]
    if manifest_digest(after) != receipt["candidate_digest"] or source_manifest(candidate) != after:
        raise HarnessError("artifact_changed", "候选内容与接受回执不一致")
    baseline = job / "source"
    verify_copy(baseline, before)
    identities = []
    for phase, source_digest in (("reproduction", manifest_digest(before)),
                                 ("verification", receipt["candidate_digest"]), ("confirmation", receipt["candidate_digest"])):
        versions = attempt.get(phase, {}).get("versions", {})
        if not versions:
            raise HarnessError("activation_evidence_missing", "验收记录缺少可启用的 runtime/模型版本事实，请重新验收")
        for value in versions.values():
            snapshot = value.get("snapshot", {})
            if snapshot.get("source_sha256") != source_digest or not value.get("model_identity"):
                raise HarnessError("activation_evidence_missing", "版本证据未绑定本次基线或候选")
            identities.append((snapshot.get("runtime"), snapshot.get("verification_sha256"), value["model_identity"]))
    if not identities[0][0] or not identities[0][1] or any(value != identities[0] for value in identities):
        raise HarnessError("activation_environment_changed", "基线、候选与独立确认的运行或评分条件不一致")
    matches = []
    for bot in (baseline / "bots").glob("*/bot.yaml"):
        app = load_application_spec(bot)
        if (app.raw.get("deploy", {}).get("instance_id") or app.raw.get("id")) == task["source"]["bot_id"]:
            matches.append(bot.relative_to(baseline).as_posix())
    if len(matches) != 1:
        raise HarnessError("activation_target_missing", "冻结源码没有唯一的应用配置")
    return ReleaseSource(task_id, task["source"]["bot_id"], baseline, before, candidate, after,
                         receipt, matches[0], identities[0][0], identities[0][2])


def resolve_runtime(repository, bot_relative, bot_id, target_id):
    from chatcopilot.plugins.runtime import require_platform
    require_platform()
    if target_id == "instance:" + bot_id:
        from chatcopilot.plugins.activation_runtime import source_target
        return source_target(repository, bot_relative, bot_id)
    parts = target_id.split(":")
    if len(parts) == 3 and parts[0] == "plugin":
        from chatcopilot.plugins.catalog import default_root
        from chatcopilot.plugins.activation_runtime import plugin_activation_runtime
        return plugin_activation_runtime(default_root(), parts[1], parts[2])
    raise ValueError("Target must be the task instance or a registered plugin:<id>:<agent>")


def prepare(controller, task_id, target_id=None, *, timeout=60):
    source = release_source(controller, task_id)
    selected = target_id or "instance:" + source.bot_id
    factory = controller.activation_factory or resolve_runtime
    runtime = factory(controller.repository, source.bot_relative, source.bot_id, selected)
    service = ActivationService(runtime, timeout=timeout, authorized=lambda: _authorized(controller, task_id))
    record = service.prepare(source)
    reference = {"target_id": selected, "bot_relative": source.bot_relative,
                 "operation_id": record["id"], "timeout": timeout}
    result = service.status(record["id"])
    controller.store.update(task_id, activation_ref=reference, activation=result)
    if not accepted(controller.store, controller.store.get(task_id)) and record["state"] == "prepared":
        service.cancel(record["id"])
        raise HarnessError("activation_cancelled", "准备期间任务已取消，未启用候选")
    return result


def operation(controller, task_id, action, *, target_id=None, timeout=60):
    if action == "activate":
        prepare(controller, task_id, target_id, timeout=timeout)
    task = controller.store.get(task_id)
    if action == "activate" and not accepted(controller.store, task):
        raise HarnessError("activation_not_accepted", "候选已失去启用资格")
    reference = task.get("activation_ref")
    if not reference:
        raise HarnessError("activation_missing", "本任务没有已准备的实例版本")
    factory = controller.activation_factory or resolve_runtime
    runtime = factory(controller.repository, reference["bot_relative"], task["source"]["bot_id"], reference["target_id"])
    service = ActivationService(runtime, timeout=reference["timeout"], authorized=lambda: _authorized(controller, task_id))
    current = service.repository.read()
    if not current or current["task_id"] != task_id:
        raise HarnessError("activation_superseded", "实例已由其他任务管理，请检查当前版本")
    ident = current["id"]
    if action == "status":
        return service.status(ident)
    if action == "reconcile":
        service.reconcile(ident, allow_desired=accepted(controller.store, task))
    else:
        getattr(service, action)(ident)
    current = service.repository.read()
    result = service.status(current["id"])
    controller.store.update(task_id, activation_ref={**reference, "operation_id": current["id"]}, activation=result)
    return result
