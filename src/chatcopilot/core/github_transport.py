"""Host-only GitHub transport and private credentials, shared by delivery adapters."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Iterator, Mapping

import requests


class GitHubError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 0) -> None:
        self.code, self.status = code, status
        super().__init__(message)


def request(token: str, method: str, path: str, *, params: Mapping[str, Any] | None = None,
            body: Mapping[str, Any] | None = None) -> Any:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("GitHub API path must be absolute")
    try:
        response = requests.request(method, "https://api.github.com" + path,
            headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "AgentStrata-delivery"},
            params=dict(params or {}), json=dict(body) if body is not None else None,
            timeout=30, allow_redirects=False)
    except requests.RequestException as exc:
        raise GitHubError("github_unavailable", "GitHub request unavailable: " + type(exc).__name__) from exc
    if not 200 <= response.status_code < 300:
        raise GitHubError("github_request_failed", f"GitHub HTTP {response.status_code}", status=response.status_code)
    if response.status_code == 204:
        return None
    try:
        payload = response.json()
    except ValueError as exc:
        raise GitHubError("github_response_invalid", "GitHub returned invalid JSON") from exc
    if path == "/graphql" and (not isinstance(payload, dict) or payload.get("errors")):
        raise GitHubError("github_graphql_failed", "GitHub rejected the GraphQL operation")
    return payload


def actor(payload: Any, expected: str) -> str:
    login = payload.get("login") if isinstance(payload, dict) else None
    if (not isinstance(login, str) or payload.get("type") != "User"
            or not re.fullmatch(r"(?=.{1,39}\Z)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", login)):
        raise GitHubError("github_response_invalid", "GitHub actor response is invalid")
    if login.casefold() != expected.casefold():
        raise GitHubError("github_actor_mismatch", "GitHub actor differs from the configured identity")
    return login


def repository_from_remote(remote: str) -> str | None:
    match = re.fullmatch(r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
                         r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", remote.strip())
    return match[1] if match else None


def load_token_file(raw: str) -> tuple[Path, str]:
    if not raw:
        raise GitHubError("github_token_missing", "GitHub token file is required")
    path = Path(raw).expanduser()
    if not path.is_absolute() or path.resolve() != path:
        raise GitHubError("github_token_invalid", "GitHub token file must be an absolute non-symlink path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise GitHubError("github_token_missing", "GitHub token file is unavailable") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise GitHubError("github_token_permissions", "GitHub token must be a single-link owned file with mode 0600")
        token = os.read(fd, 65537).decode("utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise GitHubError("github_token_missing", "GitHub token file is unreadable") from exc
    finally:
        os.close(fd)
    if not 20 <= len(token) <= 65536 or any(c.isspace() for c in token):
        raise GitHubError("github_token_invalid", "GitHub token is empty or malformed")
    return path, token


def git_environment(*, author_name: str = "", author_email: str = "") -> dict[str, str]:
    result = {key: os.environ[key] for key in ("PATH", "LANG", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
              if key in os.environ}
    result.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    if author_name:
        result.update(GIT_AUTHOR_NAME=author_name, GIT_COMMITTER_NAME=author_name)
    if author_email:
        result.update(GIT_AUTHOR_EMAIL=author_email, GIT_COMMITTER_EMAIL=author_email)
    return result


@contextmanager
def authenticated_git(token: str, directory: Path, *, author_name: str = "", author_email: str = "") -> Iterator[dict[str, str]]:
    """The secret is a transient file, never a command argument or an Agent environment."""
    with tempfile.TemporaryDirectory(prefix="git-auth-", dir=directory) as temporary:
        folder = Path(temporary)
        secret, helper = folder / "token", folder / "askpass"
        secret.write_text(token + "\n", encoding="utf-8")
        secret.chmod(0o600)
        helper.write_text('#!/bin/sh\ncase "$1" in\n*sername*) printf "%s\\n" x-access-token ;;\n'
                          '*assword*) cat "$AGENTSTRATA_GIT_SECRET_FILE" ;;\n*) exit 1 ;;\nesac\n')
        helper.chmod(0o700)
        yield {**git_environment(author_name=author_name, author_email=author_email),
               "GIT_ASKPASS": str(helper), "AGENTSTRATA_GIT_SECRET_FILE": str(secret)}
