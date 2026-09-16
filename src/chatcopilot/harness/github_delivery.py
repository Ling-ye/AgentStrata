"""GitHub adapter for reviewed Harness delivery. No candidate code is executed here."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess
from typing import Any

from chatcopilot.core import github_transport as transport
from chatcopilot.harness.config import configuration
from chatcopilot.harness.models import HarnessError


@dataclass(frozen=True)
class DeliveryConfig:
    repository: str
    actor: str
    token: str = field(repr=False)
    author_name: str = ""
    author_email: str = ""

    @classmethod
    def load(cls, values: dict[str, str] | None = None) -> DeliveryConfig:
        values = configuration() if values is None else values
        prefix = "CHATCOPILOT_HARNESS_"
        repository, actor = (values.get(prefix + key, "").strip() for key in ("GITHUB_REPOSITORY", "GITHUB_ACTOR"))
        name, email = (values.get(prefix + key, "").strip() for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"))
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not actor:
            raise HarnessError("delivery_configuration", "请配置 Harness 的 GitHub 仓库和预期账号")
        if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+", email) or any(ord(c) < 32 for c in name):
            raise HarnessError("delivery_configuration", "请配置 Harness 的公开 Git 作者身份")
        try:
            _, token = transport.load_token_file(values.get(prefix + "GITHUB_TOKEN_FILE", ""))
        except transport.GitHubError as exc:
            raise HarnessError(exc.code, str(exc)) from exc
        return cls(repository, actor, token, name, email)


def git(root: Path, *args: str, env: dict[str, str] | None = None, data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                                 "-c", "commit.gpgsign=false", "-c", "credential.helper=", "-C", str(root), *args],
                                input=data, capture_output=True, env=env or transport.git_environment(), timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError("delivery_git_unavailable", "Git 操作未确认完成，请重试对账") from exc
    if result.returncode:
        raise HarnessError("delivery_git_failed", "Git 操作失败；任务分支或远端状态需要核验")
    return result.stdout


class GitHubDelivery:
    def __init__(self, config: DeliveryConfig) -> None:
        self.config = config
        self.repo = "/repos/" + config.repository

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return transport.request(self.config.token, method, path, **kwargs)

    def graphql(self, query: str, **variables: Any) -> Any:
        return self.request("POST", "/graphql", body={"query": query, "variables": variables})["data"]

    def identity(self) -> None:
        transport.actor(self.request("GET", "/user"), self.config.actor)

    def preflight(self, repository: Path) -> dict[str, Any]:
        self.identity()
        origin = git(repository, "remote", "get-url", "origin").decode().strip()
        if transport.repository_from_remote(origin) != self.config.repository:
            raise HarnessError("delivery_origin_mismatch", "origin 与 Harness 配置的 GitHub 仓库不一致")
        info = self.request("GET", self.repo)
        if not info.get("permissions", {}).get("push") or not info.get("allow_squash_merge") or not info.get("allow_auto_merge"):
            raise HarnessError("delivery_repository_unready", "仓库需要写入权限、squash 与 auto-merge 设置")
        branch = self.request("GET", self.repo + "/branches/main")
        if not branch.get("protected"):
            raise HarnessError("delivery_branch_unprotected", "自动交付要求 main 的保护规则生效")
        sha = branch["commit"]["sha"]
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise HarnessError("delivery_remote_invalid", "远端基线身份无效")
        return {"version": 1, "state": "pending", "repository": self.config.repository, "actor": self.config.actor,
                "base_branch": "main", "base_sha": sha, "author_name": self.config.author_name,
                "author_email": self.config.author_email, "auto_merge": False}

    def verify_target(self, state: dict[str, Any]) -> None:
        if any(state.get(k) != getattr(self.config, k) for k in ("repository", "actor", "author_name", "author_email")):
            raise HarnessError("delivery_target_changed", "任务冻结的仓库、账号或作者配置已变化")
        self.identity()

    def remote_git(self, root: Path, directory: Path, *args: str) -> bytes:
        with transport.authenticated_git(self.config.token, directory, author_name=self.config.author_name,
                                         author_email=self.config.author_email) as env:
            return git(root, *args, env=env)

    @property
    def url(self) -> str:
        return "https://github.com/" + self.config.repository + ".git"

    def fetch(self, repository: Path, directory: Path, sha: str) -> None:
        self.remote_git(repository, directory, "fetch", "--no-tags", "--no-write-fetch-head", self.url, sha)
        git(repository, "cat-file", "-e", sha + "^{commit}")

    def remote_head(self, root: Path, directory: Path, branch: str) -> str | None:
        raw = self.remote_git(root, directory, "ls-remote", "--heads", self.url, "refs/heads/" + branch).decode().strip()
        if not raw:
            return None
        parts = raw.split()
        if len(parts) != 2 or parts[1] != "refs/heads/" + branch or not re.fullmatch(r"[0-9a-f]{40,64}", parts[0]):
            raise HarnessError("delivery_remote_invalid", "远端分支查询无有效身份")
        return parts[0]

    def push(self, root: Path, directory: Path, branch: str, sha: str, previous: str | None = None) -> None:
        actual = self.remote_head(root, directory, branch)
        if actual == sha:
            return
        if actual != previous:
            raise HarnessError("delivery_head_changed", "远端任务分支包含未验收的其他提交")
        self.remote_git(root, directory, "push", self.url, sha + ":refs/heads/" + branch)
        if self.remote_head(root, directory, branch) != sha:
            raise HarnessError("delivery_push_unconfirmed", "推送后的远端提交尚未核实")

    def ensure_pr(self, state: dict[str, Any], title: str, body: str) -> dict[str, Any]:
        rows = self.request("GET", self.repo + "/pulls", params={"state": "all", "head": self.config.repository.split('/')[0] + ':' + state['branch'], "per_page": 100})
        if not isinstance(rows, list) or len(rows) > 1:
            raise HarnessError("delivery_pr_ambiguous", "无法唯一确认本任务 PR")
        pr = rows[0] if rows else self.request("POST", self.repo + "/pulls", body={"title": title, "body": body,
            "head": state["branch"], "base": "main", "draft": False, "maintainer_can_modify": False})
        self.verify_pr(pr, state)
        return pr

    def verify_pr(self, pr: dict[str, Any], state: dict[str, Any]) -> None:
        if (pr.get("head", {}).get("sha") != state["commit_sha"] or pr.get("head", {}).get("ref") != state["branch"]
                or pr.get("base", {}).get("ref") != "main" or pr.get("base", {}).get("repo", {}).get("full_name") != self.config.repository
                or pr.get("head", {}).get("repo", {}).get("full_name") != self.config.repository or pr.get("draft") is not False):
            raise HarnessError("delivery_pr_changed", "PR 的仓库、分支或提交身份已变化")

    def pull(self, number: int) -> dict[str, Any]:
        return self.request("GET", self.repo + f"/pulls/{number}")

    def enable(self, state: dict[str, Any]) -> None:
        self.graphql('mutation($id:ID!,$sha:GitObjectID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,expectedHeadOid:$sha,mergeMethod:SQUASH}){pullRequest{id}}}',
                     id=state["pr_node_id"], sha=state["commit_sha"])

    def disable(self, state: dict[str, Any]) -> None:
        self.graphql('mutation($id:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$id}){pullRequest{id}}}', id=state["pr_node_id"])

    def check_summary(self, state: dict[str, Any]) -> dict[str, Any]:
        data = self.request("GET", self.repo + f"/commits/{state['commit_sha']}/check-runs", params={"per_page": 100})
        latest: dict[str, Any] = {}
        for row in data["check_runs"]:
            name = row["name"]
            if name not in latest or row["id"] > latest[name]["id"]:
                latest[name] = row
        rows = [{"name": name, "status": row["status"], "conclusion": row.get("conclusion"),
                 "url": row.get("html_url")} for name, row in latest.items()]
        return {"rows": rows, "failed": any(row["conclusion"] in {"failure", "cancelled", "timed_out", "action_required", "startup_failure"} for row in rows)}

    def merge_ready(self, state: dict[str, Any]) -> bool:
        node = self.graphql('query($id:ID!){node(id:$id){... on PullRequest {headRefOid mergeStateStatus reviewDecision}}}', id=state["pr_node_id"])["node"]
        if node.get("headRefOid") != state["commit_sha"] or node.get("mergeStateStatus") != "CLEAN":
            return False
        protection = self.request("GET", self.repo + "/branches/main/protection")
        required = protection.get("required_status_checks") or {}
        runs = []
        page = 1
        while True:
            rows = self.request("GET", self.repo + f"/commits/{state['commit_sha']}/check-runs", params={"per_page": 100, "page": page})["check_runs"]
            runs.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        statuses = self.request("GET", self.repo + f"/commits/{state['commit_sha']}/status")["statuses"]
        for check in required.get("checks", []):
            matches = [r for r in runs if r["name"] == check["context"] and (check.get("app_id") is None or r.get("app", {}).get("id") == check["app_id"])]
            if matches:
                latest = max(matches, key=lambda r: r["id"])
                if latest.get("status") != "completed" or latest.get("conclusion") != "success":
                    return False
            elif check.get("app_id") is not None or not any(s["context"] == check["context"] and s["state"] == "success" for s in statuses):
                return False
        if not required.get("checks"):
            return False
        if (protection.get("required_pull_request_reviews") or {}).get("required_approving_review_count", 0) and node.get("reviewDecision") != "APPROVED":
            return False
        return True

    def merge(self, state: dict[str, Any]) -> None:
        response = self.request("PUT", self.repo + f"/pulls/{state['pr_number']}/merge",
                                body={"sha": state["commit_sha"], "merge_method": "squash"})
        if response.get("merged") is not True:
            raise HarnessError("delivery_merge_unconfirmed", "GitHub 尚未确认合并")

    def delete_branch(self, state: dict[str, Any], root: Path, directory: Path) -> None:
        actual = self.remote_head(root, directory, state["branch"])
        if actual is None:
            return
        if actual != state["commit_sha"]:
            raise HarnessError("delivery_head_changed", "任务远端分支变化，停止删除")
        # Compare-and-swap deletion; this is not a history-rewriting push.
        self.remote_git(root, directory, "push", f"--force-with-lease=refs/heads/{state['branch']}:{actual}",
                        self.url, ":refs/heads/" + state["branch"])
        if self.remote_head(root, directory, state["branch"]) is not None:
            raise HarnessError("cleanup_remote_unconfirmed", "远端分支删除未确认")
