"""Compose existing Gateway, Harness and Evaluation entry points for a Studio project."""
import json
import os
import subprocess
import sys
import time
from functools import lru_cache

from chatcopilot.core.model_settings import worker_profile
from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.config import configuration
from chatcopilot.harness.improvement_types import ImprovementRequest
from chatcopilot.harness.models import RepairOptions
from studio.projects import socket_path


@lru_cache(maxsize=None)
def verification_runtime(state):
    """Keep one immutable verification implementation for this project's host process."""
    from pathlib import Path
    from chatcopilot.core.source_snapshot import source_manifest, manifest_digest, verify_copy
    from studio.projects import source_tree
    platform = Path(__file__).resolve().parents[1]
    manifest = source_manifest(platform)
    digest = manifest_digest(manifest)
    frozen = source_tree(state / "verification", digest, platform, manifest)
    verify_copy(frozen, manifest)
    return frozen, digest


@lru_cache(maxsize=None)
def task_verification_runtime(repo, state, task_id):
    import re
    from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
    digest = HarnessController(repo, root=state / "harness").get(task_id)["source"].get("verification_runtime")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("此任务没有独立验证快照，原记录已保留；请在同一聊天发送新的开发请求。")
    frozen = state / "verification" / digest
    if manifest_digest(source_manifest(frozen)) != digest:
        raise ValueError("任务的宿主验证快照已变化，不能继续原任务。")
    return frozen, digest


def controller(repo, state, *, start_eval=False, application_checks=(), task_id=None):
    from chatcopilot.harness.worker_runtime import SystemdWorkerControl

    verifier, verifier_digest = task_verification_runtime(repo, state, task_id) if task_id else verification_runtime(state)
    socket = socket_path(repo, verifier_digest)
    settings = configuration()
    settings["CHATCOPILOT_EVALUATION_SOCKET"] = str(socket)
    if start_eval:
        from chatcopilot.evals.service import EvaluationServiceClient, EvaluationServiceUnavailable
        client = EvaluationServiceClient(socket)
        try:
            client.health()
        except (EvaluationServiceUnavailable, FileNotFoundError):
            unit = "agentstrata-studio-eval-" + socket.stem
            command = ["systemd-run", "--user", "--quiet", "--collect", "--unit", unit,
                       "--property=UMask=0077", "--working-directory=" + str(verifier),
                       "--setenv=PYTHONPATH=" + str(verifier / "src")]
            command.extend("--setenv=" + key + "=" + settings[key] for key in
                           ("AGENTSTRATA_LLM_CONFIG", "CHATCOPILOT_CODEX_BIN", "CHATCOPILOT_CODEX_BOT_HOME")
                           if key in settings)
            command += [sys.executable, "-m", "chatcopilot.evals.service", "serve", "--repository-root", str(repo),
                        "--artifact-root", str(state / ("eval-" + socket.stem)), "--socket", str(socket),
                        "--verification-root", str(verifier)]
            launched = subprocess.run(command, capture_output=True, text=True, timeout=20)
            for _ in range(40):
                try:
                    client.health()
                    break
                except (EvaluationServiceUnavailable, FileNotFoundError):
                    time.sleep(0.25)
            else:
                raise RuntimeError("应用验证服务无法启动：" + launched.stderr[-300:])
    from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
    workers = SystemdWorkerControl(repo, state / "harness", settings, runtime_repository=verifier)
    return HarnessController(repo, root=state / "harness", evaluation_socket=socket, worker_control=workers,
                             evaluator=ServiceEvaluator(socket_path=socket, application_checks=application_checks,
                                                        verification_runtime=verifier_digest))


def stop_evaluation(repo, state, task_id):
    _, digest = task_verification_runtime(repo, state, task_id)
    subprocess.run(["systemctl", "--user", "stop", "agentstrata-studio-eval-" + socket_path(repo, digest).stem],
                   capture_output=True, timeout=20, check=False)


def start_task(repo, state, bot_id, goal, request, *, predecessor_task_id=None):
    desktop = bot_id != "live2d-desktop"
    checks = ("decision-no-tool",) if desktop else ()
    cfg, frozen = worker_profile("", environment={**os.environ, **configuration()})
    from pathlib import Path
    from chatcopilot.external_tools.codex_cli.auth_cli import status_lanes
    if not cfg.credential_root or status_lanes(Path(cfg.credential_root), "worker")[0].state != "ready":
        raise ValueError("开发账号尚未就绪。请打开「模型与账号」，点击「登录开发账号」，然后重试此请求。")
    host = controller(repo, state, start_eval=True, application_checks=checks)
    options = RepairOptions(cfg.model, cfg.reasoning_effort or "", 3, 3600, frozen, cfg.profile_id)
    scope = [p.relative_to(repo).as_posix() for p in sorted((repo / "bots" / bot_id / "prompts").glob("*.md"))]
    if desktop:
        scope.append(f"integrations/{bot_id}")
    else:
        scope.extend(("integrations/live2d_desktop/ui",
                      "integrations/open_llm_vtuber/agentstrata_vtuber/agent.py",
                      "integrations/open_llm_vtuber/agentstrata_vtuber/avatar.py"))
    suite = "agentstrata-agent-tasks-v1" if desktop else "agentstrata-application-bridges-v1"
    return host.start_improvement(ImprovementRequest("improve", bot_id, goal, tuple(scope), suite_id=suite),
                                  options, request_id=request,
                                  **({"predecessor_task_id": predecessor_task_id} if predecessor_task_id else {}))


async def discuss(home, session, request, text):
    from integrations.desktop_app.bridge import chat
    cfg = json.loads((home / "guide.json").read_text())
    from chatcopilot.project import source_notice
    suffix = source_notice()
    original = None
    show_notice = False
    retry = ("工作台无法读取上次答复。请只返回一个 JSON 对象。若这条请求明确要求制作或修改当前应用，"
             '返回 {"action":"develop","name":"应用名称","reply":"简短说明"}，工作台会把原文交给开发。'
             "讨论用 discuss，要求制作另一个应用用 new_project。本次输入如下。\n" + text)
    for key, prompt in ((request, text), (request + "-format", retry)):
        answer = await chat(cfg["url"], cfg["token"], "studio-owner", "studio-" + session, key, prompt)
        if original is None:
            original = answer
        raw = answer.strip()
        if raw.endswith("\n\n" + suffix):
            show_notice = True
            raw = raw[:-(len(suffix) + 2)].rstrip()
        if raw.startswith("```json") and raw.endswith("```"):
            raw = raw[7:-3].strip()
        try:
            decision = json.loads(raw)
        except ValueError:
            continue
        if (isinstance(decision, dict) and decision.get("action") in {"discuss", "develop", "new_project"}
                and isinstance(decision.get("reply"), str)):
            if show_notice:
                decision["reply"] += "\n\n" + suffix
            return decision
    return {"action": "discuss", "reply": original}
