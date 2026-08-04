#!/usr/bin/env python3
"""Check the public repository boundary without echoing sensitive values."""
from __future__ import annotations

import argparse
import errno
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]

# Keep this list exact and intentionally small. New public integrations should be
# reviewed before their hosts are added.
PUBLIC_URL_HOSTS = frozenset(
    {
        "abstra.io",
        "agentclientprotocol.com",
        "api.deepseek.com",
        "api.github.com",
        "api.githubcopilot.com",
        "api.openai.com",
        "api.tavily.com",
        "brave.com",
        "careers.tencent.com",
        "cli.github.com",
        "config.sinataoke.cn",
        "deb.nodesource.com",
        "developers.openai.com",
        "docs.github.com",
        "docs.python.org",
        "docs.searxng.org",
        "docs.unity3d.com",
        "en.wikipedia.org",
        "example.feishu.cn",
        "github.com",
        "gorilla.cs.berkeley.edu",
        "huggingface.co",
        "img.shields.io",
        "ilinkai.weixin.qq.com",
        "jinbao.pinduoduo.com",
        "jobs.bytedance.com",
        "keepachangelog.com",
        "learn.chatgpt.com",
        "modelcontextprotocol.io",
        "nodejs.org",
        "novac2c.cdn.weixin.qq.com",
        "npmmirror.com",
        "open.feishu.cn",
        "opencollective.com",
        "pdm-project.org",
        "pypi.org",
        "pyarmor.readthedocs.io",
        "python-poetry.org",
        "raw.githubusercontent.com",
        "registry.modelcontextprotocol.io",
        "registry.npmmirror.com",
        "registry.npmjs.org",
        "semver.org",
        "tarkov.dev",
        "tavily.com",
        "tidelift.com",
        "union.jd.com",
        "upr.unity.cn",
        "webarena.dev",
        "www.alimama.com",
        "www.contributor-covenant.org",
        "www.minimaxi.com",
        "www.nowcoder.com",
        "www.npmjs.com",
        "www.swebench.com",
        "www.w3.org",
        "www.xiaohongshu.com",
        "x.feishu.cn",
        "xx.feishu.cn",
        "xxx.feishu.cn",
    }
)
SAFE_LOCAL_URL_HOSTS = frozenset(
    {
        "0.0.0.0",
        "127.0.0.1",
        "::",
        "::1",
        "localhost",
        "proxy",
        "searxng",
        "wsl" + ".localhost",
    }
)
TEST_FIXTURE_PRIVATE_HOSTS = frozenset(
    {".".join(("10", "0", "0", "1")), ".".join(("192", "168", "1", "1"))}
)
PUBLIC_EMAILS = frozenset({"616202172@qq.com"})
PUBLIC_EMAIL_DOMAINS = frozenset({"users.noreply.github.com"})
NON_EMAIL_ADDRESS_TOKENS = frozenset({"git@github.com"})
# Generic user names are not evidence that a machine path is public.
GENERIC_MACHINE_USERS: frozenset[str] = frozenset()
FORBIDDEN_BACKUP_SUFFIXES = (".orig", ".rej", "~", ".bak", ".swp", ".swo")
FORBIDDEN_BACKUP_PATHSPECS = tuple(
    f"*{suffix}" for suffix in FORBIDDEN_BACKUP_SUFFIXES
)

# Coordinates are exact. Fixture coordinates are explicit rather than owner-wide.
PUBLIC_CODE_REPOSITORIES = frozenset(
    {
        "github.com/acme/project",
        "github.com/arc53/docsgpt",
        "github.com/brave/brave-search-mcp-server",
        "github.com/example/docs",
        "github.com/example/sample",
        "github.com/github/github-mcp-server",
        "github.com/gitleaks/gitleaks",
        "github.com/gollum/gollum",
        "github.com/google-research/google-research",
        "github.com/kubernetes/enhancements",
        "github.com/ling-ye/agentstrata",
        "github.com/liuliang520530/taoke-mcp",
        "github.com/microsoft/playwright-mcp",
        "github.com/mkdocs/mkdocs",
        "github.com/modelcontextprotocol/servers",
        "github.com/newren/git-filter-repo",
        "github.com/onyx-dot-app/onyx",
        "github.com/other/project",
        "github.com/python/peps",
        "github.com/run-llama/llama_index",
        "github.com/rust-lang/rfcs",
        "github.com/vitejs/vite",
        "github.com/xpzouying/xiaohongshu-mcp",
    }
)
GITHUB_NON_REPOSITORY_NAMESPACES = frozenset(
    {
        "about", "apps", "collections", "contact", "events", "explore", "features",
        "issues", "login", "marketplace", "new", "notifications", "orgs", "pricing",
        "pulls", "search", "security", "settings", "site", "sponsors", "topics",
    }
)

URL_RE = re.compile(r"""(?i)\bhttps?://[^\s<>"'`]+""")
URI_RE = re.compile(
    r"""(?ix)\b(?:wss?|ssh|git|git\+ssh|"""
    r"""postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|"""
    r"""amqp|amqps|kafka|mssql|sqlserver|clickhouse|elasticsearch)"""
    r"""://[^\s<>"'`]+"""
)
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63})(?![A-Za-z0-9.-])"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:DSA |EC |ENCRYPTED |OPENSSH |PGP |RSA )?"
    r"PRIVATE KEY(?: BLOCK)?-----"
)
UNIX_MACHINE_PATH_RE = re.compile(
    r"""(?<![A-Za-z0-9_.-])/(?:home|Users)/"""
    r"""(?P<user>[A-Za-z0-9._-]+)(?:/[^\s<>"'`]+)?"""
)
WINDOWS_MACHINE_PATH_RE = re.compile(
    r"""(?i)(?<![A-Za-z0-9])(?:[A-Z]:[/\\]+|/mnt/[a-z]/)"""
    r"""(?:Users|Documents[ ]and[ ]Settings)[/\\]+"""
    r"""(?P<user>[A-Za-z0-9._-]+)(?:[/\\]+[^\s<>"'`]+)?"""
)
GITHUB_REPO_RE = re.compile(
    r"(?i)(?:https?://(?:www\.)?github\.com/|git@github\.com:|(?<![A-Za-z0-9.])github\.com/)"
    r"(?P<owner>Ling-ye)/(?P<repo>[A-Za-z0-9_.-]+)"
)
GITHUB_API_REPO_RE = re.compile(
    r"(?i)https?://api\.github\.com/repos/"
    r"(?P<owner>Ling-ye)/(?P<repo>[A-Za-z0-9_.-]+)"
)
GITHUB_RAW_REPO_RE = re.compile(
    r"(?i)https?://raw\.githubusercontent\.com/"
    r"(?P<owner>Ling-ye)/(?P<repo>[A-Za-z0-9_.-]+)"
)
ANY_GITHUB_REPO_RE = re.compile(
    r"(?i)(?:https?://(?:www\.)?github\.com/|git@github\.com:|"
    r"ssh://git@github\.com/|(?<![A-Za-z0-9.])github\.com/)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
)
ANY_GITHUB_API_REPO_RE = re.compile(
    r"(?i)https?://api\.github\.com/repos/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
)
ANY_GITHUB_RAW_REPO_RE = re.compile(
    r"(?i)https?://raw\.githubusercontent\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
)
ANY_GITHUB_CODELOAD_REPO_RE = re.compile(
    r"(?i)https?://codeload\.github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
)
OTHER_CODE_REPO_RE = re.compile(
    r"(?i)(?:https?|ssh|git|git\+ssh)://"
    r"(?P<host>(?:www\.)?(?:gitlab\.com|bitbucket\.org|codeberg\.org|gitee\.com))/"
    r"(?P<coordinate>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)"
)
SCP_CODE_REPO_RE = re.compile(
    r"(?i)(?:git|hg)@"
    r"(?P<host>gitlab\.com|bitbucket\.org|codeberg\.org|gitee\.com):"
    r"(?P<coordinate>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)"
)
GIT_IDENTITY_HEADER_RE = re.compile(
    r"^(?P<kind>author|committer|tagger) (?P<prefix>.*<)"
    r"(?P<email>[^<>\s]+@[^<>\s]+)(?P<suffix>>[^\r\n]*)"
    r"(?P<ending>\r?\n?)$"
)
ALLOWED_SYSTEMD_UNIT_TOKEN_RE = re.compile(
    r"(?:cc-connect|chatcopilot|chatcopilot-code-worker)"
    r"@[a-z0-9][a-z0-9-]{0,62}\.service"
    r"|user@[0-9]+\.service"
)
ALLOWED_SYSTEMD_UNIT_PATH_PREFIXES = (
    "console/systemd/",
    "deploy/wsl/",
    "docs/",
    "specs/",
)
PRIVATE_HOST_SUFFIXES = (
    ".corp", ".home", ".internal", ".intranet", ".lan", ".local", ".localhost"
)
BARE_PRIVATE_HOST_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])"
    r"(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
    r"(?:\.corp|\.home|\.internal|\.intranet|\.lan|\.local|\.localhost))"
    r"(?![A-Za-z0-9_.(-])"
)
BARE_IP_RE = re.compile(
    r"(?<![A-Za-z0-9_.:])(?P<host>(?:\d{1,3}\.){3}\d{1,3})(?![A-Za-z0-9_.:])"
)
BARE_IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:])(?P<host>(?:[0-9A-Fa-f]{0,4}:){2,7}"
    r"[0-9A-Fa-f]{0,4})(?![0-9A-Fa-f:])"
)
GENERIC_URI_USERNAMES = frozenset({"anonymous", "git", "hg", "svn"})
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token", "api_key", "apikey", "auth", "client_secret", "credential",
        "key", "passwd", "password", "secret", "session", "sig", "signature",
        "ticket", "token",
    }
)
DOCUMENT_HOST_MARKERS = {
    "feishu.cn": frozenset({"base", "docs", "docx", "file", "sheets", "wiki"}),
    "larksuite.com": frozenset({"base", "docs", "docx", "file", "sheets", "wiki"}),
    "docs.google.com": frozenset({"document", "forms", "presentation", "spreadsheets"}),
    "drive.google.com": frozenset({"file", "folders"}),
}
PUBLIC_DOCUMENT_HOSTS = frozenset(
    {"example.feishu.cn", "open.feishu.cn", "x.feishu.cn", "xx.feishu.cn", "xxx.feishu.cn"}
)
INDEX_BLOB_MODES = frozenset({"100644", "100755", "120000"})
INDEX_GITLINK_MODE = "160000"
TREE_MODES = frozenset({"40000", "040000"})
DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "192.0." + "2.0/24",
        "198.51." + "100.0/24",
        "203.0." + "113.0/24",
        "2001:" + "db8" + "::/32",
    )
)


class PublicRepoCheckError(RuntimeError):
    """Raised when the repository cannot be scanned safely."""


@dataclass(frozen=True, order=True)
class Finding:
    rule: str
    path: str
    line: int
    digest: str


@dataclass(frozen=True, order=True)
class IndexEntry:
    path: str
    mode: str
    oid: str


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    name: str
    oid: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _finding(rule: str, path: str, line: int, value: str) -> Finding:
    return Finding(rule=rule, path=_safe_path(path), line=line, digest=_digest(value))


def _safe_path(path: str) -> str:
    printable = "".join(character if character.isprintable() else "?" for character in path)
    return printable[:300]


def _is_reserved_example_host(host: str) -> bool:
    if host in {"example", "invalid", "test"}:
        return True
    if host.endswith((".example", ".invalid", ".test")):
        return True
    return any(
        host == base or host.endswith(f".{base}")
        for base in ("example.com", "example.net", "example.org")
    )


def _is_templated_host(host: str) -> bool:
    return any(character in host for character in ("$", "\\", "{", "}")) or bool(
        re.search(r"%(?:s|\([A-Za-z_][A-Za-z0-9_]*\)s)", host)
    )


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().casefold()
    if not normalized:
        return True
    if any(character in value for character in ("$", "{", "}", "<", ">")):
        return True
    return normalized in {
        "changeme", "dummy", "example", "placeholder", "redacted", "replace-me",
        "sample", "test", "token", "xxx",
    } or normalized.startswith(("example_", "sample_", "test_", "your_", "replace_"))


def _is_documentation_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.casefold().strip("[]").rstrip("."))
    except ValueError:
        return False
    return any(address in network for network in DOCUMENTATION_NETWORKS)


def _is_private_or_local_host(host: str) -> bool:
    normalized = host.casefold().strip("[]").rstrip(".")
    if "." not in normalized and ":" not in normalized:
        return True
    if normalized.endswith(PRIVATE_HOST_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(
        (address.is_private, address.is_loopback, address.is_link_local,
         address.is_reserved, address.is_unspecified)
    )


def _relative_scan_path(path: str) -> str:
    for prefix in ("index:", "worktree:", "untracked:", "history-name:"):
        if path.startswith(prefix):
            return path[len(prefix) :].split("@", 1)[0]
    if path.startswith("history:"):
        return path[len("history:") :].rsplit("@", 1)[0]
    return path


def _is_known_test_fixture(path: str, host: str) -> bool:
    return host in TEST_FIXTURE_PRIVATE_HOSTS and _relative_scan_path(path).startswith("tests/")


def _is_allowed_systemd_unit_token(value: str, *, path: str) -> bool:
    relative_path = _relative_scan_path(path)
    return relative_path.startswith(ALLOWED_SYSTEMD_UNIT_PATH_PREFIXES) and (
        ALLOWED_SYSTEMD_UNIT_TOKEN_RE.fullmatch(value) is not None
    )


def _is_allowed_email(email: str, *, path: str) -> bool:
    normalized = email.casefold()
    if normalized in PUBLIC_EMAILS or normalized in NON_EMAIL_ADDRESS_TOKENS:
        return True
    _, _, domain = normalized.rpartition("@")
    if domain in PUBLIC_EMAIL_DOMAINS or _is_reserved_example_host(domain):
        return True
    return _is_allowed_systemd_unit_token(normalized, path=path)


def _normalized_url_host(value: str) -> str | None:
    candidate = value.rstrip(".,;:!?)]}")
    try:
        host = urlsplit(candidate).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.casefold().rstrip(".")


def _document_finding(candidate: str, *, path: str, line_number: int) -> Finding | None:
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host or host in PUBLIC_DOCUMENT_HOSTS:
        return None
    marker_set: frozenset[str] | None = None
    for suffix, markers in DOCUMENT_HOST_MARKERS.items():
        if host == suffix or host.endswith(f".{suffix}"):
            marker_set = markers
            break
    if marker_set is None:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.casefold() in marker_set and not _is_placeholder(parts[index + 1]):
            return _finding("private-document-identifier", path, line_number, host)
    return None


def _scan_uri_candidate(candidate: str, *, path: str, line_number: int) -> set[Finding]:
    findings: set[Finding] = set()
    candidate = candidate.rstrip(".,;:!?)]}")
    try:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").casefold().rstrip(".")
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
    except ValueError:
        if any(character in candidate for character in ("$", "\\", "{", "}")):
            return findings
        findings.add(_finding("invalid-uri", path, line_number, candidate))
        return findings
    if not host or _is_templated_host(host):
        return findings
    if (
        username
        and username.casefold() not in GENERIC_URI_USERNAMES
        and not _is_placeholder(username)
    ):
        findings.add(
            _finding(
                "uri-userinfo-identity-or-secret",
                path,
                line_number,
                username,
            )
        )
    if password and not _is_placeholder(password):
        findings.add(_finding("uri-userinfo-secret", path, line_number, password))
    for query in (parsed.query, parsed.fragment if "=" in parsed.fragment else ""):
        for key, value in parse_qsl(query, keep_blank_values=True):
            if key.casefold() not in SENSITIVE_QUERY_KEYS:
                continue
            if not value:
                findings.add(
                    _finding(
                        "sensitive-uri-query",
                        path,
                        line_number,
                        f"{key}=",
                    )
                )
            elif not _is_placeholder(value):
                findings.add(
                    _finding("sensitive-uri-query", path, line_number, value)
                )
    document_finding = _document_finding(candidate, path=path, line_number=line_number)
    if document_finding is not None:
        findings.add(document_finding)
    if (
        host not in PUBLIC_URL_HOSTS
        and host not in SAFE_LOCAL_URL_HOSTS
        and not _is_reserved_example_host(host)
        and not _is_documentation_address(host)
        and not _is_known_test_fixture(path, host)
    ):
        rule = "url-private-or-local-host" if _is_private_or_local_host(host) else "url-host-not-allowlisted"
        findings.add(_finding(rule, path, line_number, host))
    return findings


def _scan_urls(line: str, *, path: str, line_number: int) -> set[Finding]:
    findings: set[Finding] = set()
    for pattern in (URL_RE, URI_RE):
        for match in pattern.finditer(line):
            findings.update(_scan_uri_candidate(match.group(0), path=path, line_number=line_number))
    return findings


def _normalized_repository(host: str, coordinate: str) -> str:
    normalized_host = host.casefold().removeprefix("www.")
    normalized_coordinate = coordinate.rstrip(".,;:!?)]}").removesuffix(".git")
    return f"{normalized_host}/{normalized_coordinate.casefold()}"


def _scan_maintainer_repositories(line: str, *, path: str, line_number: int) -> set[Finding]:
    findings: set[Finding] = set()
    github_patterns = (
        ANY_GITHUB_REPO_RE, ANY_GITHUB_API_REPO_RE,
        ANY_GITHUB_RAW_REPO_RE, ANY_GITHUB_CODELOAD_REPO_RE,
    )
    for pattern in github_patterns:
        for match in pattern.finditer(line):
            owner = match.group("owner").casefold()
            if owner in GITHUB_NON_REPOSITORY_NAMESPACES:
                continue
            repository = match.group("repo").removesuffix(".git")
            identity = _normalized_repository("github.com", f"{owner}/{repository}")
            if identity in PUBLIC_CODE_REPOSITORIES:
                continue
            rule = "unexpected-maintainer-github-repo" if owner == "ling-ye" else "unexpected-code-repository"
            findings.add(_finding(rule, path, line_number, identity))
    for pattern in (OTHER_CODE_REPO_RE, SCP_CODE_REPO_RE):
        for match in pattern.finditer(line):
            identity = _normalized_repository(match.group("host"), match.group("coordinate"))
            if identity not in PUBLIC_CODE_REPOSITORIES:
                findings.add(_finding("unexpected-code-repository", path, line_number, identity))
    return findings


def _covered(position: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def _scan_bare_private_hosts(line: str, *, path: str, line_number: int) -> set[Finding]:
    spans = [match.span() for pattern in (URL_RE, URI_RE) for match in pattern.finditer(line)]
    findings: set[Finding] = set()
    for pattern in (BARE_PRIVATE_HOST_RE, BARE_IP_RE, BARE_IPV6_RE):
        for match in pattern.finditer(line):
            if _covered(match.start(), spans):
                continue
            host = match.group("host").casefold().strip("[]").rstrip(".")
            if pattern in {BARE_IP_RE, BARE_IPV6_RE}:
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    continue
                if pattern is BARE_IPV6_RE:
                    before = line[match.start() - 1] if match.start() else ""
                    after = line[match.end()] if match.end() < len(line) else ""
                    if (
                        before.isalpha()
                        or before in "_."
                        or after.isalpha()
                        or after in "_{"
                    ):
                        continue
                if not _is_private_or_local_host(host):
                    continue
            if (
                host in SAFE_LOCAL_URL_HOSTS
                or _is_documentation_address(host)
                or _is_known_test_fixture(path, host)
            ):
                continue
            findings.add(_finding("bare-private-or-local-host", path, line_number, host))
    return findings


def scan_text(
    text: str,
    *,
    path: str,
    private_literals: Sequence[str] = (),
    check_identity_metadata: bool = True,
) -> list[Finding]:
    findings: set[Finding] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        findings.update(_scan_urls(line, path=path, line_number=line_number))
        if check_identity_metadata:
            findings.update(
                _scan_maintainer_repositories(
                    line, path=path, line_number=line_number
                )
            )
        findings.update(_scan_bare_private_hosts(line, path=path, line_number=line_number))
        for match in PRIVATE_KEY_RE.finditer(line):
            findings.add(_finding("private-key-header", path, line_number, match.group(0)))
        if check_identity_metadata:
            for match in EMAIL_RE.finditer(line):
                email = match.group(1)
                if not _is_allowed_email(email, path=path):
                    findings.add(_finding("unexpected-email", path, line_number, email))
        for pattern in (UNIX_MACHINE_PATH_RE, WINDOWS_MACHINE_PATH_RE):
            for match in pattern.finditer(line):
                findings.add(_finding("machine-user-path", path, line_number, match.group(0)))
        for literal in private_literals:
            if literal in line:
                findings.add(_finding("private-literal", path, line_number, literal))
    return sorted(findings)

def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ("git", *args), cwd=root, input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
    except FileNotFoundError as exc:
        raise PublicRepoCheckError("git executable is unavailable") from exc
    if completed.returncode != 0:
        command = args[0] if args else "<unknown>"
        raise PublicRepoCheckError(f"git {command} failed")
    return completed.stdout


def _decode_git_path(raw_path: bytes) -> str:
    try:
        path = raw_path.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PublicRepoCheckError("git returned a non-UTF-8 path") from exc
    pure_path = PurePosixPath(path)
    if (
        not path or pure_path.is_absolute()
        or any(part in {"", ".", ".."} for part in pure_path.parts)
    ):
        raise PublicRepoCheckError("git returned an unsafe repository path")
    return path


def _path_list(root: Path, *args: str) -> tuple[str, ...]:
    output = _git(root, "ls-files", *args, "-z")
    return tuple(_decode_git_path(raw) for raw in output.split(b"\0") if raw)


def _backup_artifact_paths(root: Path, *, ignored: bool) -> tuple[str, ...]:
    visibility_args = (
        ("--others", "--ignored", "--exclude-standard")
        if ignored
        else ("--cached", "--others", "--exclude-standard")
    )
    return _path_list(root, *visibility_args, *FORBIDDEN_BACKUP_PATHSPECS)


def _index_entries(root: Path) -> tuple[IndexEntry, ...]:
    output = _git(root, "ls-files", "--cached", "--stage", "-z")
    entries: list[IndexEntry] = []
    seen_paths: set[str] = set()
    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue
        metadata, separator, raw_path = raw_entry.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise PublicRepoCheckError("git returned a malformed index entry")
        try:
            mode = fields[0].decode("ascii", errors="strict")
            oid = fields[1].decode("ascii", errors="strict")
            stage = fields[2].decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise PublicRepoCheckError("git returned a malformed index entry") from exc
        path = _decode_git_path(raw_path)
        if stage != "0" or path in seen_paths:
            raise PublicRepoCheckError("the index contains unresolved entries")
        if mode not in INDEX_BLOB_MODES and mode != INDEX_GITLINK_MODE:
            raise PublicRepoCheckError("the index contains an unsupported entry type")
        if not re.fullmatch(r"[0-9a-f]+", oid) or set(oid) == {"0"}:
            raise PublicRepoCheckError("the index contains an unresolved object")
        seen_paths.add(path)
        entries.append(IndexEntry(path=path, mode=mode, oid=oid))
    return tuple(entries)


def _tracked_paths(root: Path) -> list[str]:
    """Compatibility helper returning current candidate path names."""
    paths = {entry.path for entry in _index_entries(root)}
    paths.update(_path_list(root, "--others", "--exclude-standard"))
    return sorted(paths)


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return all(
        getattr(left, attribute) == getattr(right, attribute)
        for attribute in (
            "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"
        )
    )


def _worktree_bytes(
    root: Path,
    relative_path: str,
    *,
    allow_gitlink_directory: bool = False,
) -> bytes | None:
    """Read one worktree entity without following links; None means gitlink dir."""
    path = root / relative_path
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise PublicRepoCheckError("a worktree candidate disappeared during scanning") from exc
    except OSError as exc:
        raise PublicRepoCheckError("unable to inspect a worktree candidate") from exc
    if stat.S_ISLNK(before.st_mode):
        try:
            target = os.readlink(path)
            after = path.lstat()
        except OSError as exc:
            raise PublicRepoCheckError("unable to read a worktree symbolic link") from exc
        if not _same_file_state(before, after):
            raise PublicRepoCheckError("a worktree candidate changed during scanning")
        return os.fsencode(target)
    if stat.S_ISDIR(before.st_mode) and allow_gitlink_directory:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise PublicRepoCheckError("a worktree candidate has an unsupported type")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOENT}:
            raise PublicRepoCheckError("a worktree candidate changed during scanning") from exc
        raise PublicRepoCheckError("unable to open a worktree candidate") from exc
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        try:
            after = path.lstat()
        except OSError as exc:
            raise PublicRepoCheckError("a worktree candidate changed during scanning") from exc
    finally:
        os.close(descriptor)
    if not (
        _same_file_state(before, opened)
        and _same_file_state(opened, finished)
        and _same_file_state(finished, after)
    ):
        raise PublicRepoCheckError("a worktree candidate changed during scanning")
    return b"".join(chunks)


def _object_types(root: Path, object_ids: Sequence[str]) -> dict[str, str]:
    if not object_ids:
        return {}
    request = "".join(f"{oid}\n" for oid in object_ids).encode("ascii")
    output = _git(
        root, "cat-file", "--batch-check=%(objectname) %(objecttype)",
        input_bytes=request,
    )
    lines = output.splitlines()
    if len(lines) != len(object_ids):
        raise PublicRepoCheckError("git cat-file returned incomplete object metadata")
    types: dict[str, str] = {}
    for expected_oid, raw_line in zip(object_ids, lines, strict=True):
        try:
            fields = raw_line.decode("ascii", errors="strict").split()
        except UnicodeDecodeError as exc:
            raise PublicRepoCheckError("git cat-file returned invalid object metadata") from exc
        if len(fields) != 2 or fields[0] != expected_oid or fields[1] == "missing":
            raise PublicRepoCheckError("git cat-file returned invalid object metadata")
        types[expected_oid] = fields[1]
    return types


def _chunks(values: Sequence[str], size: int = 64) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _batch_contents(root: Path, object_ids: Sequence[str]) -> Iterable[tuple[str, bytes]]:
    for chunk in _chunks(object_ids):
        request = "".join(f"{oid}\n" for oid in chunk).encode("ascii")
        output = _git(root, "cat-file", "--batch", input_bytes=request)
        offset = 0
        for expected_oid in chunk:
            header_end = output.find(b"\n", offset)
            if header_end < 0:
                raise PublicRepoCheckError("git cat-file returned a truncated header")
            fields = output[offset:header_end].split()
            if len(fields) != 3:
                raise PublicRepoCheckError("git cat-file returned an invalid header")
            oid = fields[0].decode("ascii", errors="strict")
            try:
                size = int(fields[2])
            except ValueError as exc:
                raise PublicRepoCheckError("git cat-file returned an invalid size") from exc
            content_start = header_end + 1
            content_end = content_start + size
            if content_end >= len(output):
                raise PublicRepoCheckError("git cat-file returned truncated content")
            if oid != expected_oid or output[content_end : content_end + 1] != b"\n":
                raise PublicRepoCheckError("git cat-file response did not match its request")
            yield oid, output[content_start:content_end]
            offset = content_end + 1
        if offset != len(output):
            raise PublicRepoCheckError("git cat-file returned unexpected trailing data")


def _scan_bytes(
    content: bytes,
    *,
    path: str,
    private_literals: Sequence[str] = (),
) -> set[Finding]:
    return set(
        scan_text(
            content.decode("utf-8", errors="replace"),
            path=path,
            private_literals=private_literals,
        )
    )


def _relax_git_identity_header_emails(text: str, *, object_type: str) -> str:
    headers, separator, message = text.partition("\n\n")
    allowed_kinds = (
        {"author", "committer"} if object_type == "commit" else {"tagger"}
    )
    relaxed_lines: list[str] = []
    for line in headers.splitlines(keepends=True):
        match = GIT_IDENTITY_HEADER_RE.fullmatch(line)
        if match is None or match.group("kind") not in allowed_kinds:
            relaxed_lines.append(line)
            continue
        relaxed_lines.append(
            f"{match.group('kind')} {match.group('prefix')}"
            f"identity@users.noreply.github.com{match.group('suffix')}"
            f"{match.group('ending')}"
        )
    return "".join(relaxed_lines) + separator + message


def _strict_git_identity_findings(
    text: str,
    *,
    path: str,
    object_type: str,
) -> set[Finding]:
    headers, _, _ = text.partition("\n\n")
    allowed_kinds = (
        {"author", "committer"} if object_type == "commit" else {"tagger"}
    )
    findings: set[Finding] = set()
    for line_number, line in enumerate(headers.splitlines(keepends=True), start=1):
        match = GIT_IDENTITY_HEADER_RE.fullmatch(line)
        if match is None or match.group("kind") not in allowed_kinds:
            continue
        email = match.group("email")
        if not _is_allowed_email(email, path=path):
            findings.add(
                _finding("unexpected-email", path, line_number, email)
            )
    return findings


def _scan_git_metadata(
    content: bytes,
    *,
    path: str,
    object_type: str,
    strict_git_identities: bool,
    private_literals: Sequence[str] = (),
) -> set[Finding]:
    text = content.decode("utf-8", errors="replace")
    findings: set[Finding] = set()
    if strict_git_identities:
        findings.update(
            _strict_git_identity_findings(
                text,
                path=path,
                object_type=object_type,
            )
        )
    else:
        text = _relax_git_identity_header_emails(text, object_type=object_type)
    findings.update(
        scan_text(
            text,
            path=path,
            private_literals=private_literals,
            check_identity_metadata=False,
        )
    )
    return findings


def _scan_path_name(
    relative_path: str,
    *,
    path: str,
    private_literals: Sequence[str] = (),
) -> set[Finding]:
    findings = set(
        scan_text(relative_path, path=path, private_literals=private_literals)
    )
    name = PurePosixPath(relative_path).name
    if name.endswith(FORBIDDEN_BACKUP_SUFFIXES):
        findings.add(
            _finding("forbidden-backup-artifact", path, 1, relative_path)
        )
    return findings


def scan_tracked(
    root: Path = ROOT,
    *,
    private_literals: Sequence[str] = (),
) -> list[Finding]:
    findings: set[Finding] = set()
    index_before = _index_entries(root)
    modified_before = _path_list(root, "--modified")
    deleted_before = frozenset(_path_list(root, "--deleted"))
    untracked_before = _path_list(root, "--others", "--exclude-standard")
    backup_before = _backup_artifact_paths(root, ignored=False)
    ignored_backup_before = _backup_artifact_paths(root, ignored=True)
    index_paths = {entry.path for entry in index_before}

    for relative_path in backup_before:
        source_kind = "index" if relative_path in index_paths else "untracked"
        findings.update(
            _scan_path_name(
                relative_path,
                path=f"{source_kind}:{relative_path}",
                private_literals=private_literals,
            )
        )
    for relative_path in ignored_backup_before:
        findings.update(
            _scan_path_name(
                relative_path,
                path=f"ignored:{relative_path}",
                private_literals=private_literals,
            )
        )

    blob_entries = [entry for entry in index_before if entry.mode in INDEX_BLOB_MODES]
    blob_oids = tuple(dict.fromkeys(entry.oid for entry in blob_entries))
    object_types = _object_types(root, blob_oids)
    if any(object_types.get(oid) != "blob" for oid in blob_oids):
        raise PublicRepoCheckError("the index references a non-blob object")
    blob_contents = dict(_batch_contents(root, blob_oids))
    for entry in index_before:
        source = f"index:{entry.path}"
        findings.update(
            _scan_path_name(
                entry.path,
                path=source,
                private_literals=private_literals,
            )
        )
        if entry.mode in INDEX_BLOB_MODES:
            findings.update(
                _scan_bytes(
                    blob_contents[entry.oid],
                    path=source,
                    private_literals=private_literals,
                )
            )

    gitlink_paths = {entry.path for entry in index_before if entry.mode == INDEX_GITLINK_MODE}
    for relative_path in modified_before:
        source = f"worktree:{relative_path}"
        findings.update(
            _scan_path_name(
                relative_path,
                path=source,
                private_literals=private_literals,
            )
        )
        try:
            content = _worktree_bytes(
                root, relative_path,
                allow_gitlink_directory=relative_path in gitlink_paths,
            )
        except PublicRepoCheckError:
            if relative_path in deleted_before and not (root / relative_path).exists():
                continue
            raise
        if content is not None:
            findings.update(
                _scan_bytes(content, path=source, private_literals=private_literals)
            )

    for relative_path in untracked_before:
        source = f"untracked:{relative_path}"
        findings.update(
            _scan_path_name(
                relative_path,
                path=source,
                private_literals=private_literals,
            )
        )
        content = _worktree_bytes(root, relative_path)
        if content is None:
            raise PublicRepoCheckError("an untracked candidate has an unsupported type")
        findings.update(
            _scan_bytes(content, path=source, private_literals=private_literals)
        )

    index_after = _index_entries(root)
    modified_after = _path_list(root, "--modified")
    deleted_after = frozenset(_path_list(root, "--deleted"))
    untracked_after = _path_list(root, "--others", "--exclude-standard")
    backup_after = _backup_artifact_paths(root, ignored=False)
    ignored_backup_after = _backup_artifact_paths(root, ignored=True)
    if (
        index_before != index_after
        or modified_before != modified_after
        or deleted_before != deleted_after
        or untracked_before != untracked_after
        or backup_before != backup_after
        or ignored_backup_before != ignored_backup_after
    ):
        raise PublicRepoCheckError("repository candidates changed during scanning")
    return sorted(findings)

def _reachable_objects(root: Path) -> tuple[list[str], dict[str, str]]:
    """Compatibility inventory; paths are hints and not used for strict history scans."""
    output = _git(root, "rev-list", "--objects", "--all")
    object_ids: list[str] = []
    paths: dict[str, str] = {}
    seen: set[str] = set()
    for raw_line in output.splitlines():
        raw_oid, separator, raw_path = raw_line.partition(b" ")
        try:
            oid = raw_oid.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise PublicRepoCheckError("git returned an invalid object identifier") from exc
        if not re.fullmatch(r"[0-9a-f]+", oid):
            raise PublicRepoCheckError("git returned an invalid object identifier")
        if oid not in seen:
            seen.add(oid)
            object_ids.append(oid)
        if separator and raw_path and oid not in paths:
            paths[oid] = _decode_git_path(raw_path)
    return object_ids, paths


def _history_path(oid: str, object_type: str, paths: dict[str, str]) -> str:
    if object_type == "blob":
        relative_path = paths.get(oid, "<unknown>")
        return f"history:{relative_path}@{oid[:12]}"
    return f"{object_type}:{oid[:12]}"


def _parse_tree(content: bytes, oid_length: int) -> tuple[TreeEntry, ...]:
    entries: list[TreeEntry] = []
    offset = 0
    raw_oid_length = oid_length // 2
    while offset < len(content):
        space = content.find(b" ", offset)
        nul = content.find(b"\0", space + 1) if space >= 0 else -1
        if space <= offset or nul <= space + 1:
            raise PublicRepoCheckError("git returned a malformed tree object")
        raw_mode = content[offset:space]
        raw_name = content[space + 1 : nul]
        oid_end = nul + 1 + raw_oid_length
        if oid_end > len(content):
            raise PublicRepoCheckError("git returned a truncated tree object")
        try:
            mode = raw_mode.decode("ascii", errors="strict")
            name = raw_name.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PublicRepoCheckError("git returned an invalid tree entry") from exc
        if mode not in INDEX_BLOB_MODES | TREE_MODES | {INDEX_GITLINK_MODE}:
            raise PublicRepoCheckError("git returned an unsupported tree entry")
        if not name or name in {".", ".."} or "/" in name:
            raise PublicRepoCheckError("git returned an unsafe tree entry")
        entry_oid = content[nul + 1 : oid_end].hex()
        entries.append(TreeEntry(mode=mode, name=name, oid=entry_oid))
        offset = oid_end
    return tuple(entries)


def _header_value(content: bytes, name: bytes) -> str | None:
    prefix = name + b" "
    for line in content.splitlines():
        if not line:
            break
        if line.startswith(prefix):
            try:
                return line[len(prefix) :].decode("ascii", errors="strict")
            except UnicodeDecodeError as exc:
                raise PublicRepoCheckError("git returned malformed object headers") from exc
    return None


def _history_tree_paths(
    object_types: dict[str, str],
    contents: dict[str, bytes],
) -> tuple[dict[str, set[str]], set[str]]:
    tree_ids = {oid for oid, object_type in object_types.items() if object_type == "tree"}
    if not tree_ids:
        return {}, set()
    oid_length = len(next(iter(object_types)))
    tree_entries = {oid: _parse_tree(contents[oid], oid_length) for oid in tree_ids}
    child_tree_ids = {
        entry.oid for entries in tree_entries.values() for entry in entries
        if entry.mode in TREE_MODES
    }
    roots = tree_ids - child_tree_ids
    for oid, object_type in object_types.items():
        if object_type == "commit":
            tree_oid = _header_value(contents[oid], b"tree")
            if tree_oid is None or object_types.get(tree_oid) != "tree":
                raise PublicRepoCheckError("git returned a commit without a valid tree")
            roots.add(tree_oid)
        elif object_type == "tag" and _header_value(contents[oid], b"type") == "tree":
            tree_oid = _header_value(contents[oid], b"object")
            if tree_oid is None or object_types.get(tree_oid) != "tree":
                raise PublicRepoCheckError("git returned a tag without a valid tree")
            roots.add(tree_oid)

    blob_paths: dict[str, set[str]] = {}
    all_paths: set[str] = set()
    visited: set[tuple[str, str]] = set()

    def visit(tree_oid: str, prefix: str, ancestors: frozenset[str]) -> None:
        key = (tree_oid, prefix)
        if key in visited:
            return
        if tree_oid in ancestors:
            raise PublicRepoCheckError("git tree graph contains a cycle")
        visited.add(key)
        next_ancestors = ancestors | {tree_oid}
        for entry in tree_entries[tree_oid]:
            relative_path = f"{prefix}/{entry.name}" if prefix else entry.name
            _decode_git_path(os.fsencode(relative_path))
            all_paths.add(relative_path)
            if entry.mode in TREE_MODES:
                if object_types.get(entry.oid) != "tree":
                    raise PublicRepoCheckError("git tree references a missing tree")
                visit(entry.oid, relative_path, next_ancestors)
            elif entry.mode in INDEX_BLOB_MODES:
                if object_types.get(entry.oid) != "blob":
                    raise PublicRepoCheckError("git tree references a missing blob")
                blob_paths.setdefault(entry.oid, set()).add(relative_path)

    for root_oid in sorted(roots):
        visit(root_oid, "", frozenset())
    return blob_paths, all_paths


def scan_history(
    root: Path = ROOT,
    *,
    strict_git_identities: bool = False,
    private_literals: Sequence[str] = (),
) -> list[Finding]:
    object_ids, paths = _reachable_objects(root)
    object_types = _object_types(root, object_ids)
    supported_types = {"blob", "commit", "tag", "tree"}
    if any(object_type not in supported_types for object_type in object_types.values()):
        raise PublicRepoCheckError("reachable history contains an unsupported object type")
    contents = dict(_batch_contents(root, object_ids))
    blob_paths, history_paths = _history_tree_paths(object_types, contents)

    findings: set[Finding] = set()
    for relative_path in sorted(history_paths):
        findings.update(
            _scan_path_name(
                relative_path,
                path=f"history-name:{relative_path}",
                private_literals=private_literals,
            )
        )
    for oid in object_ids:
        object_type = object_types[oid]
        if object_type in {"commit", "tag"}:
            findings.update(
                _scan_git_metadata(
                    contents[oid],
                    path=_history_path(oid, object_type, paths),
                    object_type=object_type,
                    strict_git_identities=strict_git_identities,
                    private_literals=private_literals,
                )
            )
        elif object_type == "blob":
            mapped_paths = blob_paths.get(oid)
            if not mapped_paths:
                findings.update(
                    _scan_bytes(
                        contents[oid],
                        path=_history_path(oid, object_type, paths),
                        private_literals=private_literals,
                    )
                )
                continue
            text = contents[oid].decode("utf-8", errors="replace")
            for relative_path in sorted(mapped_paths):
                findings.update(
                    scan_text(
                        text,
                        path=f"history:{relative_path}@{oid[:12]}",
                        private_literals=private_literals,
                    )
                )
    return sorted(findings)


def render_finding(
    finding: Finding,
    *,
    location_id: str = "location-opaque",
    finding_id: str = "finding-opaque",
) -> str:
    return f"rule={finding.rule} location={location_id} line={finding.line} finding={finding_id}"


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def load_private_literals(
    literals_path: Path,
    *,
    root: Path = ROOT,
) -> tuple[str, ...]:
    """Load exact private values from one external owner-only regular file."""
    if not literals_path.is_absolute() or literals_path.name in {"", ".", ".."}:
        raise PublicRepoCheckError(
            "private literals path must be an absolute file path"
        )
    try:
        resolved_root = root.resolve(strict=True)
        before = literals_path.lstat()
        resolved_path = literals_path.resolve(strict=True)
    except OSError as exc:
        raise PublicRepoCheckError("private literals file is unavailable") from exc
    if _is_within(resolved_path, resolved_root):
        raise PublicRepoCheckError(
            "private literals file must be outside the repository"
        )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
    ):
        raise PublicRepoCheckError(
            "private literals file must be an owner-only single-link regular file"
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(literals_path, flags)
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        after = literals_path.lstat()
    except OSError as exc:
        raise PublicRepoCheckError(
            "unable to read the private literals file safely"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not (
        _same_file_state(before, opened)
        and _same_file_state(opened, finished)
        and _same_file_state(finished, after)
        and stat.S_ISREG(finished.st_mode)
        and finished.st_uid == os.geteuid()
        and stat.S_IMODE(finished.st_mode) == 0o600
        and finished.st_nlink == 1
    ):
        raise PublicRepoCheckError("private literals file changed during reading")

    try:
        text = b"".join(chunks).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PublicRepoCheckError("private literals file must be valid UTF-8") from exc
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or any(line == "" for line in lines):
        raise PublicRepoCheckError(
            "private literals file must contain one non-empty literal per line"
        )
    if any(
        unicodedata.category(character) == "Cc"
        for line in lines
        for character in line
    ):
        raise PublicRepoCheckError(
            "private literals file contains a control character"
        )
    if len(set(lines)) != len(lines):
        raise PublicRepoCheckError("private literals file contains duplicate entries")
    return tuple(lines)


def _write_private_report(
    report_path: Path,
    findings: Sequence[Finding],
    *,
    root: Path,
) -> None:
    if not report_path.is_absolute() or report_path.name in {"", ".", ".."}:
        raise PublicRepoCheckError("private report path must be an absolute file path")
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, flag) for flag in required_flags):
        raise PublicRepoCheckError("secure private report creation is unavailable")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise PublicRepoCheckError("repository root is unavailable") from exc

    directory_descriptor = -1
    report_descriptor = -1
    created_identity: tuple[int, int] | None = None
    success = False
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_descriptor = os.open(report_path.parent, directory_flags)
        directory_stat = os.fstat(directory_descriptor)
        try:
            actual_parent = Path(
                f"/proc/self/fd/{directory_descriptor}"
            ).resolve(strict=True)
            actual_parent_stat = actual_parent.stat()
        except OSError as exc:
            raise PublicRepoCheckError("private report directory is unavailable") from exc
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or (directory_stat.st_dev, directory_stat.st_ino)
            != (actual_parent_stat.st_dev, actual_parent_stat.st_ino)
        ):
            raise PublicRepoCheckError("private report directory is unavailable")
        if _is_within(actual_parent, resolved_root):
            raise PublicRepoCheckError(
                "private report directory must be outside the repository"
            )
        if (
            directory_stat.st_uid != os.geteuid()
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise PublicRepoCheckError("private report directory must be owner-only")

        report_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        report_flags |= getattr(os, "O_CLOEXEC", 0)
        report_descriptor = os.open(
            report_path.name,
            report_flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(report_descriptor)
        created_identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
        ):
            raise PublicRepoCheckError(
                "private report is not an owner-only regular file"
            )
        payload = "".join(
            json.dumps(
                {
                    "digest": finding.digest,
                    "line": finding.line,
                    "path": finding.path,
                    "rule": finding.rule,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
            + "\n"
            for finding in findings
        ).encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(report_descriptor, payload[offset:])
            if written <= 0:
                raise PublicRepoCheckError("private report write did not progress")
            offset += written
        os.fsync(report_descriptor)
        finished = os.fstat(report_descriptor)
        entry = os.stat(
            report_path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        directory_finished = os.fstat(directory_descriptor)
        try:
            actual_parent_finished = Path(
                f"/proc/self/fd/{directory_descriptor}"
            ).resolve(strict=True)
            actual_parent_finished_stat = actual_parent_finished.stat()
        except OSError as exc:
            raise PublicRepoCheckError(
                "private report directory changed during writing"
            ) from exc
        if (
            (directory_finished.st_dev, directory_finished.st_ino)
            != (directory_stat.st_dev, directory_stat.st_ino)
            or (actual_parent_finished_stat.st_dev, actual_parent_finished_stat.st_ino)
            != (directory_stat.st_dev, directory_stat.st_ino)
            or directory_finished.st_uid != os.geteuid()
            or stat.S_IMODE(directory_finished.st_mode) != 0o700
            or _is_within(actual_parent_finished, resolved_root)
        ):
            raise PublicRepoCheckError(
                "private report directory changed during writing"
            )
        if (
            (finished.st_dev, finished.st_ino) != created_identity
            or (entry.st_dev, entry.st_ino) != created_identity
            or finished.st_uid != opened.st_uid
            or stat.S_IMODE(finished.st_mode) != 0o600
            or finished.st_nlink != 1
            or finished.st_size != len(payload)
        ):
            raise PublicRepoCheckError("private report entry changed during writing")
        success = True
    except PublicRepoCheckError:
        raise
    except OSError as exc:
        raise PublicRepoCheckError("unable to create the private report safely") from exc
    finally:
        if report_descriptor >= 0:
            os.close(report_descriptor)
        if not success and created_identity is not None and directory_descriptor >= 0:
            try:
                entry = os.stat(
                    report_path.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (entry.st_dev, entry.st_ino) == created_identity:
                    os.unlink(report_path.name, dir_fd=directory_descriptor)
            except OSError:
                pass
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check public content without printing matched values or paths."
    )
    parser.add_argument(
        "--history", action="store_true",
        help="scan all reachable blobs, paths, commit metadata/messages, and tags",
    )
    parser.add_argument(
        "--strict-git-identities",
        action="store_true",
        help="treat author, committer, and tagger header emails as strict findings",
    )
    parser.add_argument(
        "--private-report", type=Path,
        help=("write internal locations and digests to a new 0600 file in an external "
              "owner-only directory"),
    )
    parser.add_argument(
        "--private-literals-file",
        type=Path,
        help=(
            "scan exact values loaded from an external owner-only 0600 file; "
            "matched values are never printed"
        ),
    )
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        private_literals = (
            load_private_literals(args.private_literals_file, root=args.root)
            if args.private_literals_file is not None
            else ()
        )
        findings = (
            scan_history(
                args.root,
                strict_git_identities=args.strict_git_identities,
                private_literals=private_literals,
            )
            if args.history
            else scan_tracked(args.root, private_literals=private_literals)
        )
        if args.private_report is not None:
            _write_private_report(args.private_report, findings, root=args.root)
    except PublicRepoCheckError as exc:
        print(f"ERROR: {exc}")
        return 2

    if not findings:
        scope = "reachable history" if args.history else "index, worktree, and untracked tree"
        print(f"OK: public repository boundary ({scope})")
        return 0
    location_ids: dict[str, str] = {}
    for finding_number, finding in enumerate(findings, start=1):
        if finding.path not in location_ids:
            location_ids[finding.path] = f"location-{len(location_ids) + 1:04d}"
        print(
            render_finding(
                finding,
                location_id=location_ids[finding.path],
                finding_id=f"finding-{finding_number:04d}",
            )
        )
    print(f"FAIL: {len(findings)} public repository boundary finding(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
