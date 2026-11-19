"""Connect a Studio project to its own verified Evaluation service."""

import json
import re

from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_directory, private_file
from chatcopilot.core.source_snapshot import git_output, manifest_digest, source_manifest
from chatcopilot.evals.service import EvaluationServiceClient
from studio import projects, qq_sources, runtime


class ProjectIssue(ValueError):
    """A project-scoped prerequisite or conflict safe to show to the operator."""


def target(apps, row):
    project = row["project_id"]
    if row["source"] != "studio" or not project:
        raise ProjectIssue("请先导入该应用可验证的开发源码")
    studio = apps.studio()
    if row["type"] == "qq_official":
        manager = apps.manager()
        selected = qq_sources.resolve(manager, project)
        if selected is None:
            raise ProjectIssue("请先在本应用的开发对话中准备 QQ 源码")
        repo, record = selected
        return repo, manager._root(project) / "development" / record["digest"], "qq-app-" + project
    with studio.db.connect() as db:
        source = db.execute("SELECT bot,version FROM projects WHERE id=?", (project,)).fetchone()
    if not source or not re.fullmatch(r"initial|[0-9a-f]{64}", source["version"]):
        raise ProjectIssue("请先在开发对话中确认方案并建立应用源码")
    state = studio.state_dir / "projects" / project
    repo = state / source["version"]
    if not repo.is_dir() or repo.resolve() != repo:
        raise ProjectIssue("此应用的开发源码不可用")
    return repo, state, source["bot"]


def connect(apps, row):
    project = row["project_id"]
    repo, state, bot = target(apps, row)
    if row["type"] == "qq_official":
        host = apps.manager().source_controller(project, start_eval=True)
    else:
        if (repo.name == "initial" and git_output(repo, "status", "--porcelain")) or (
            repo.name != "initial" and manifest_digest(source_manifest(repo)) != repo.name
        ):
            raise ProjectIssue("应用源码已偏离开发基线，请重新核对")
        studio = apps.studio()
        host = runtime.controller(repo, state, start_eval=True, **studio.environment())
    # Only the host-derived verification identity is persisted. The browser supplies no paths or sockets.
    verifier = host.evaluator.verification_runtime
    write_private(
        private_directory(state) / "console-evaluation.json",
        json.dumps(
            {
                "version": repo.name,
                "verifier": verifier,
                "source_digest": manifest_digest(source_manifest(repo)),
            }
        ),
    )
    return bot


def connection(apps, row, *, verify_source=False):
    repo, state, bot = target(apps, row)
    receipt = state / "console-evaluation.json"
    if not receipt.is_file():
        raise ProjectIssue("请连接本应用的测评服务")
    private_file(receipt)
    data = json.loads(receipt.read_text())
    verifier = data.get("verifier", "")
    if data.get("version") != repo.name or not re.fullmatch(r"[0-9a-f]{64}", verifier):
        raise ProjectIssue("应用基线已变化，请重新连接测评服务")
    if verify_source and manifest_digest(source_manifest(repo)) != data.get("source_digest"):
        raise ProjectIssue("应用源码与已连接的测评基线不匹配")
    sock = projects.socket_path(repo, verifier, create=False)
    client = EvaluationServiceClient(sock)
    health = client.health()
    expected = {
        "repository_root": str(repo),
        "verification_root": str(state / "verification" / verifier),
        "artifact_root": str(state / ("eval-" + sock.stem)),
    }
    if any(health.get(k) != v for k, v in expected.items()):
        raise ProjectIssue("测评服务与当前应用的源码或验证基线不匹配")
    return client, bot
