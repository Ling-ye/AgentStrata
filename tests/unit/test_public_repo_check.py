from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_checker():
    script = ROOT / "scripts" / "check_public_repo.py"
    spec = importlib.util.spec_from_file_location("check_public_repo", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Lingye")
    _git(repo, "config", "user.email", "616202172" + "@" + "qq.com")
    return repo


def _commit_all(repo: Path, message: str = "测试提交") -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)


def _write(repo: Path, relative_path: str, text: str) -> None:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_current_scan_flags_privacy_rules_without_publicly_rendering_values(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    private_host = "corp" + ".internal"
    unknown_host = "downloads" + ".vendor.tld"
    unexpected_email = "alice" + "@" + "company.com"
    machine_path = "/home/" + "alice/private"
    generic_machine_path = "/home/" + "developer/project"
    windows_machine_path = "C:" + "\\Users\\" + "Alice\\private"
    private_key = "-----BEGIN " + "PRIVATE KEY-----"
    private_repo = "https://github.com/" + "Ling-ye" + "/private-project"
    contents = "\n".join(
        (
            f"https://{private_host}/api",
            f"https://{unknown_host}/release",
            unexpected_email,
            machine_path,
            generic_machine_path,
            windows_machine_path,
            private_key,
            private_repo,
        )
    )
    _write(repo, "tracked.txt", contents)
    _commit_all(repo)

    findings = checker.scan_tracked(repo)

    assert {finding.rule for finding in findings} == {
        "machine-user-path",
        "private-key-header",
        "unexpected-email",
        "unexpected-maintainer-github-repo",
        "url-host-not-allowlisted",
        "url-private-or-local-host",
    }
    assert {finding.path for finding in findings} == {"index:tracked.txt"}
    rendered = "\n".join(checker.render_finding(finding) for finding in findings)
    for sensitive_value in (
        private_host,
        unknown_host,
        unexpected_email,
        machine_path,
        generic_machine_path,
        windows_machine_path,
        private_key,
        "private-project",
        "tracked.txt",
    ):
        assert sensitive_value not in rendered
    assert "digest=" not in rendered
    assert all(len(finding.digest) == 64 for finding in findings)


def test_current_scan_allows_exact_public_and_placeholder_values(tmp_path: Path) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    allowed_email = "616202172" + "@" + "qq.com"
    example_email = "developer" + "@" + "sample.example.invalid"
    contents = "\n".join(
        (
            "https://github.com/" + "Ling-ye" + "/AgentStrata",
            "https://github.com/gitleaks/gitleaks/releases",
            "git@github.com:gitleaks/gitleaks.git",
            "ssh://git@github.com/gitleaks/gitleaks.git",
            "https://api.github.com/repos/gitleaks/gitleaks",
            "https://raw.githubusercontent.com/gitleaks/gitleaks/master/README.md",
            "https://github.com/features/actions",
            "https://api.openai.com/v1/models?token=${TOKEN}",
            "https://assets.example.invalid/file",
            "http://localhost:3000/health",
            "redis://localhost/0",
            "https://example.feishu.cn/wiki/example-token",
            allowed_email,
            example_email,
            f"`{allowed_email}`",
            "artifact=" + "@sample.zip",
            "cc-connect" + "@" + "bot.service",
        )
    )
    _write(repo, "docs/public.md", contents)
    _commit_all(repo)

    assert checker.scan_tracked(repo) == []


@pytest.mark.parametrize(
    ("path", "unit"),
    [
        (
            "index:docs/operations.md",
            "chatcopilot" + "@" + "demo-bot.service",
        ),
        (
            "worktree:deploy/wsl/README_WSL.md",
            "chatcopilot-code-worker" + "@" + "demo-bot.service",
        ),
        (
            "untracked:console/systemd/example.md",
            "cc-connect" + "@" + "demo-bot.service",
        ),
        (
            "history:specs/example/spec.md@deadbeef",
            "user" + "@" + "1000.service",
        ),
    ],
)
def test_systemd_unit_tokens_are_exact_and_path_scoped(
    path: str,
    unit: str,
) -> None:
    checker = _load_checker()

    assert checker.scan_text(unit, path=path) == []


@pytest.mark.parametrize(
    "suffix",
    [".service", ".socket", ".target", ".timer", ".path"],
)
def test_arbitrary_email_like_systemd_suffixes_are_not_allowed(
    suffix: str,
) -> None:
    checker = _load_checker()
    email = "person" + "@" + "private" + suffix

    findings = checker.scan_text(email, path="index:docs/operations.md")

    assert {finding.rule for finding in findings} == {"unexpected-email"}


def test_project_systemd_unit_token_is_rejected_outside_documentation_paths() -> None:
    checker = _load_checker()
    unit = "chatcopilot" + "@" + "demo-bot.service"

    findings = checker.scan_text(unit, path="index:src/example.py")

    assert {finding.rule for finding in findings} == {"unexpected-email"}


def test_current_scan_keeps_index_worktree_and_untracked_sources_separate(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    _write(repo, "staged.txt", "https://example.invalid/safe\n")
    _write(repo, "tracked.txt", "https://example.invalid/safe\n")
    _commit_all(repo)

    staged_host = "staged" + ".internal"
    worktree_host = "worktree" + ".internal"
    untracked_host = "untracked" + ".internal"
    _write(repo, "staged.txt", f"https://{staged_host}/secret\n")
    _git(repo, "add", "staged.txt")
    _write(repo, "staged.txt", "https://example.invalid/safe-again\n")
    _write(repo, "tracked.txt", f"https://{worktree_host}/secret\n")
    _write(repo, "candidate.txt", f"https://{untracked_host}/secret\n")

    findings = checker.scan_tracked(repo)
    host_findings = {
        (finding.path, finding.digest)
        for finding in findings
        if finding.rule == "url-private-or-local-host"
    }

    assert host_findings == {
        ("index:staged.txt", checker._digest(staged_host)),
        ("worktree:tracked.txt", checker._digest(worktree_host)),
        ("untracked:candidate.txt", checker._digest(untracked_host)),
    }
    assert all(finding.path != "worktree:staged.txt" for finding in findings)


def test_current_scan_ignores_ignored_untracked_content(tmp_path: Path) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    _write(repo, ".gitignore", "local.env\n")
    _write(repo, "tracked.txt", "https://example.invalid/public\n")
    _commit_all(repo)
    _write(repo, "local.env", "https://" + "private" + ".internal/api\n")

    assert checker.scan_tracked(repo) == []


def test_private_literals_file_scans_current_tree_without_rendering_values(
    tmp_path: Path,
    capsys,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    private_literal = "tenant" + "-private-marker"
    _write(repo, "tracked.txt", f"prefix={private_literal}\n")
    _commit_all(repo)
    literals_path = tmp_path / "private-literals.txt"
    literals_path.write_text(private_literal + "\n", encoding="utf-8")
    literals_path.chmod(0o600)

    assert checker.main(
        [
            "--root",
            str(repo),
            "--private-literals-file",
            str(literals_path),
        ]
    ) == 1

    output = capsys.readouterr().out
    assert "rule=private-literal" in output
    assert private_literal not in output
    assert "tracked.txt" not in output
    findings = checker.scan_tracked(
        repo,
        private_literals=checker.load_private_literals(literals_path, root=repo),
    )
    assert [finding.rule for finding in findings] == ["private-literal"]
    assert findings[0].digest == checker._digest(private_literal)


def test_private_literals_file_scans_deleted_history(tmp_path: Path) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    private_literal = "removed" + "-private-marker"
    _write(repo, "removed.txt", private_literal + "\n")
    _commit_all(repo)
    (repo / "removed.txt").unlink()
    _write(repo, "current.txt", "safe\n")
    _commit_all(repo)
    literals_path = tmp_path / "private-literals.txt"
    literals_path.write_text(private_literal, encoding="utf-8")
    literals_path.chmod(0o600)
    private_literals = checker.load_private_literals(literals_path, root=repo)

    assert checker.scan_tracked(repo, private_literals=private_literals) == []
    findings = checker.scan_history(repo, private_literals=private_literals)
    assert any(
        finding.rule == "private-literal"
        and finding.path.startswith("history:removed.txt@")
        for finding in findings
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("", "non-empty"),
        ("first\n\nsecond\n", "non-empty"),
        ("duplicate\nduplicate\n", "duplicate"),
        ("tab\tvalue\n", "control"),
        ("windows\r\n", "control"),
    ],
)
def test_private_literals_file_rejects_invalid_content(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    literals_path = tmp_path / "private-literals.txt"
    literals_path.write_text(payload, encoding="utf-8", newline="")
    literals_path.chmod(0o600)

    with pytest.raises(checker.PublicRepoCheckError, match=message):
        checker.load_private_literals(literals_path, root=repo)


def test_private_literals_file_rejects_unsafe_location_and_file_types(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    inside = repo / "private-literals.txt"
    inside.write_text("private-marker", encoding="utf-8")
    inside.chmod(0o600)
    with pytest.raises(checker.PublicRepoCheckError, match="outside"):
        checker.load_private_literals(inside, root=repo)

    open_mode = tmp_path / "open-mode.txt"
    open_mode.write_text("private-marker", encoding="utf-8")
    open_mode.chmod(0o644)
    with pytest.raises(checker.PublicRepoCheckError, match="owner-only"):
        checker.load_private_literals(open_mode, root=repo)

    target = tmp_path / "target.txt"
    target.write_text("private-marker", encoding="utf-8")
    target.chmod(0o600)
    linked = tmp_path / "linked.txt"
    linked.symlink_to(target)
    with pytest.raises(checker.PublicRepoCheckError, match="single-link"):
        checker.load_private_literals(linked, root=repo)

    hard_link = tmp_path / "hard-link.txt"
    os.link(target, hard_link)
    with pytest.raises(checker.PublicRepoCheckError, match="single-link"):
        checker.load_private_literals(target, root=repo)


def test_current_scan_fails_closed_for_unsupported_untracked_type(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    os.mkfifo(repo / "candidate.pipe")
    real_path_list = checker._path_list

    def candidate_paths(root: Path, *args: str) -> tuple[str, ...]:
        if args == ("--others", "--exclude-standard"):
            return ("candidate.pipe",)
        return real_path_list(root, *args)

    monkeypatch.setattr(checker, "_path_list", candidate_paths)
    with pytest.raises(checker.PublicRepoCheckError, match="unsupported type"):
        checker.scan_tracked(repo)


def test_filename_is_scanned_in_current_tree_and_history(tmp_path: Path) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    sensitive_directory = "tenant" + ".internal"
    _write(repo, f"{sensitive_directory}/safe.txt", "safe\n")
    _commit_all(repo)

    current = checker.scan_tracked(repo)
    history = checker.scan_history(repo)

    assert any(
        finding.rule == "bare-private-or-local-host"
        and finding.path == f"index:{sensitive_directory}/safe.txt"
        for finding in current
    )
    assert any(
        finding.rule == "bare-private-or-local-host"
        and finding.path == f"history-name:{sensitive_directory}/safe.txt"
        for finding in history
    )


def test_history_scan_includes_deleted_blobs_and_commit_author_metadata(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    private_host = "history" + ".internal"
    old_email = "former" + "@" + "company.com"
    _write(repo, "removed.txt", f"https://{private_host}/secret\n")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Former Developer",
        "-c",
        f"user.email={old_email}",
        "commit",
        "-q",
        "-m",
        "historical privacy fixture",
    )
    (repo / "removed.txt").unlink()
    _write(repo, "current.txt", "https://example.invalid/public\n")
    _commit_all(repo, "replace historical fixture")
    tag_host = "tagged" + ".internal"
    _git(repo, "tag", "-a", "v0.1.0", "-m", f"https://{tag_host}/release")

    assert checker.scan_tracked(repo) == []
    findings = checker.scan_history(repo, strict_git_identities=True)

    assert any(
        finding.rule == "url-private-or-local-host"
        and finding.path.startswith("history:removed.txt@")
        for finding in findings
    )
    assert any(
        finding.rule == "unexpected-email" and finding.path.startswith("commit:")
        for finding in findings
    )
    assert any(
        finding.rule == "url-private-or-local-host" and finding.path.startswith("tag:")
        for finding in findings
    )


def test_history_uses_every_path_for_a_shared_blob(tmp_path: Path) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    fixture_host = ".".join(("10", "0", "0", "1"))
    content = f"endpoint={fixture_host}\n"
    _write(repo, "tests/fixture.txt", content)
    _write(repo, "config/runtime.txt", content)
    _commit_all(repo)

    findings = checker.scan_history(repo)
    bare_findings = [
        finding for finding in findings
        if finding.rule == "bare-private-or-local-host"
        and finding.path.startswith("history:")
    ]

    assert any("history:config/runtime.txt@" in finding.path for finding in bare_findings)
    assert not any("history:tests/fixture.txt@" in finding.path for finding in bare_findings)


def test_non_http_private_endpoint_query_document_and_repo_rules() -> None:
    checker = _load_checker()
    private_ipv4 = ".".join(("10", "23", "45", "67"))
    private_ipv6 = "fd12" + ":3456::1"
    private_socket_host = "socket" + ".internal"
    database_host = "db" + ".internal"
    database_password = "real-" + "secret"
    private_owner = "private" + "-org"
    gateway_host = "gateway" + ".corp"
    sensitive_query = "real-secret-" + "123"
    tenant_host = "tenant" + ".feishu.cn"
    document_id = "private-" + "document-id"
    text = "\n".join(
        (
            f"wss://{private_socket_host}/connect",
            f"postgresql://svc:{database_password}@{database_host}/app",
            f"ssh://git@github.com/{private_owner}/private-repo.git",
            private_ipv4,
            private_ipv6,
            gateway_host,
            f"https://api.openai.com/v1?access_token={sensitive_query}",
            f"https://{tenant_host}/wiki/{document_id}",
            f"https://github.com/{private_owner}/private-repo",
            "git" + f"@gitlab.com:{private_owner}/team/repo.git",
            "/home/" + "user/private",
        )
    )

    rules = {finding.rule for finding in checker.scan_text(text, path="candidate.txt")}

    assert {
        "bare-private-or-local-host",
        "machine-user-path",
        "private-document-identifier",
        "sensitive-uri-query",
        "unexpected-code-repository",
        "uri-userinfo-secret",
        "url-host-not-allowlisted",
        "url-private-or-local-host",
    } <= rules


def test_empty_sensitive_uri_query_is_blocked() -> None:
    checker = _load_checker()
    sensitive_key = "access_" + "token"
    findings = checker.scan_text(
        f"https://example.invalid/api?{sensitive_key}=",
        path="candidate.txt",
    )

    assert {finding.rule for finding in findings} == {"sensitive-uri-query"}
    assert findings[0].digest == checker._digest(f"{sensitive_key}=")


def test_history_email_has_no_digest_bypass() -> None:
    checker = _load_checker()
    assert checker.GENERIC_MACHINE_USERS == frozenset()
    assert not hasattr(checker, "ALLOWED_HISTORY_MACHINE_PATH_DIGESTS")
    assert not hasattr(checker, "ALLOWED_HISTORY_EMAIL_DIGESTS")

    email_value = "fixture" + "@" + "company.com"
    for path in ("candidate.txt", "history:fixture.txt@deadbeef"):
        findings = checker.scan_text(email_value, path=path)
        assert {finding.rule for finding in findings} == {"unexpected-email"}


def test_cli_output_is_opaque_and_private_report_is_owner_only(
    tmp_path: Path,
    capsys,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    sensitive_host = "release." + "private" + ".lan"
    _write(repo, "sensitive-name.txt", f"https://{sensitive_host}/artifact\n")
    _commit_all(repo)
    report_directory = tmp_path / "private-report"
    report_directory.mkdir(mode=0o700)
    report_path = report_directory / "findings.jsonl"

    assert checker.main(
        ["--root", str(repo), "--private-report", str(report_path)]
    ) == 1

    output = capsys.readouterr().out
    assert sensitive_host not in output
    assert "sensitive-name.txt" not in output
    assert checker._digest(sensitive_host) not in output
    assert "rule=url-private-or-local-host" in output
    assert "location=location-0001" in output
    assert "finding=finding-0001" in output
    assert "line=1" in output
    assert "digest=" not in output and "path=" not in output

    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    private_text = report_path.read_text(encoding="utf-8")
    private_record = json.loads(private_text)
    assert private_record["path"] == "index:sensitive-name.txt"
    assert private_record["digest"] == checker._digest(sensitive_host)
    assert sensitive_host not in private_text


def test_private_report_rejects_repository_and_non_owner_only_directories(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    finding = checker._finding("test-rule", "private/path", 1, "secret-value")

    with pytest.raises(checker.PublicRepoCheckError, match="outside"):
        checker._write_private_report(repo / "report.jsonl", [finding], root=repo)

    open_directory = tmp_path / "open-report"
    open_directory.mkdir(mode=0o755)
    with pytest.raises(checker.PublicRepoCheckError, match="owner-only"):
        checker._write_private_report(open_directory / "report.jsonl", [finding], root=repo)

def test_private_report_rejects_symlink_directory(tmp_path: Path) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    finding = checker._finding("test-rule", "private/path", 1, "secret-value")
    real_directory = tmp_path / "real-private"
    real_directory.mkdir(mode=0o700)
    linked_directory = tmp_path / "linked-private"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(checker.PublicRepoCheckError):
        checker._write_private_report(
            linked_directory / "report.jsonl",
            [finding],
            root=repo,
        )
    assert not (real_directory / "report.jsonl").exists()


def test_private_report_does_not_unlink_replacement_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    finding = checker._finding("test-rule", "private/path", 1, "secret-value")
    report_directory = tmp_path / "private-race"
    report_directory.mkdir(mode=0o700)
    report_path = report_directory / "report.jsonl"
    moved_path = report_directory / "moved-original.jsonl"
    real_write = checker.os.write
    swapped = False

    def replace_directory_entry(descriptor: int, payload: bytes) -> int:
        nonlocal swapped
        written = real_write(descriptor, payload)
        if not swapped:
            report_path.rename(moved_path)
            report_path.write_text("replacement", encoding="utf-8")
            swapped = True
        return written

    monkeypatch.setattr(checker.os, "write", replace_directory_entry)
    with pytest.raises(checker.PublicRepoCheckError, match="entry changed"):
        checker._write_private_report(report_path, [finding], root=repo)

    assert report_path.read_text(encoding="utf-8") == "replacement"
    assert moved_path.exists()
    assert report_path.stat().st_ino != moved_path.stat().st_ino


def test_private_report_removes_its_own_entry_after_write_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    finding = checker._finding("test-rule", "private/path", 1, "secret-value")
    report_directory = tmp_path / "private-failure"
    report_directory.mkdir(mode=0o700)
    report_path = report_directory / "report.jsonl"

    def fail_sync(_descriptor: int) -> None:
        raise OSError("simulated")

    monkeypatch.setattr(checker.os, "fsync", fail_sync)
    with pytest.raises(checker.PublicRepoCheckError, match="safely"):
        checker._write_private_report(report_path, [finding], root=repo)
    assert not report_path.exists()

def test_bare_address_scan_ignores_invalid_tokens_and_documentation_networks() -> None:
    checker = _load_checker()
    documentation_ipv4 = ".".join(("192", "0", "2", "42"))
    documentation_ipv6 = "2001" + ":db8::42"
    text = "\n".join(
        (
            "duration=:52:",
            "version=999.999.999.999",
            f"address={documentation_ipv4}",
            f"address={documentation_ipv6}",
            "http" + f"://{documentation_ipv4}/example",
            "http" + f"://[{documentation_ipv6}]/example",
        )
    )

    assert checker.scan_text(text, path="candidate.txt") == []

def test_bare_host_scan_ignores_code_members_and_wsl_unc_host() -> None:
    checker = _load_checker()
    wsl_unc = "\\\\wsl." + "localhost\\Ubuntu\\workspace"
    text = "\n".join(
        (
            "home = Path.home()",
            "empty = System.String::Empty",
            ".item::before { color: red; }",
            wsl_unc,
            "http://%s:%s/dynamic-endpoint",
            "http://%(host)s:%(port)s/dynamic-endpoint",
        )
    )

    assert checker.scan_text(text, path="candidate.txt") == []

def test_uri_token_as_username_is_blocked_on_an_allowed_host() -> None:
    checker = _load_checker()
    token_username = "token-part-" + "0123456789"
    findings = checker.scan_text(
        f"https://{token_username}@api.openai.com/v1/models",
        path="candidate.txt",
    )

    assert any(
        finding.rule == "uri-userinfo-identity-or-secret"
        for finding in findings
    )
    assert checker.scan_text(
        "ssh://git@github.com/gitleaks/gitleaks.git",
        path="candidate.txt",
    ) == []


def test_history_git_identity_is_optional_and_dependency_metadata_is_public(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    external_email = "contributor" + "@" + "outside.example.dev"
    _write(repo, "first.txt", "safe\n")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=External Contributor",
        "-c",
        f"user.email={external_email}",
        "commit",
        "-q", "-m", "\n".join(
            (
                "safe contribution",
                "",
                "Updates https://github.com/" + "upstream/project.",
                "Signed-off-by: Contributor <support" + "@" + "github.com>",
            )
        ),
    )

    default_findings = checker.scan_history(repo)
    strict_findings = checker.scan_history(repo, strict_git_identities=True)
    assert not any(finding.rule == "unexpected-email" for finding in default_findings)
    assert sum(
        finding.rule == "unexpected-email" for finding in strict_findings
    ) == 2

    _write(repo, "second.txt", "safe\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", f"contact {external_email}")
    assert not any(
        finding.rule == "unexpected-email"
        for finding in checker.scan_history(repo)
    )

def test_private_report_detects_parent_moved_into_repository(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    finding = checker._finding("test-rule", "private/path", 1, "secret-value")
    report_directory = tmp_path / "private-parent-race"
    report_directory.mkdir(mode=0o700)
    report_path = report_directory / "report.jsonl"
    moved_directory = repo / "moved-private-report"
    real_sync = checker.os.fsync
    moved = False

    def move_parent_after_sync(descriptor: int) -> None:
        nonlocal moved
        real_sync(descriptor)
        if not moved:
            report_directory.rename(moved_directory)
            moved = True

    monkeypatch.setattr(checker.os, "fsync", move_parent_after_sync)
    with pytest.raises(checker.PublicRepoCheckError, match="directory changed"):
        checker._write_private_report(report_path, [finding], root=repo)
    assert not (moved_directory / "report.jsonl").exists()

def test_current_scan_blocks_backup_artifacts_in_all_git_visibility_scopes(
    tmp_path: Path,
    capsys,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    _write(repo, ".gitignore", "*.orig\n*.swo\n")
    _write(repo, "safe.txt", "safe\n")
    _commit_all(repo)

    _write(repo, "tracked.orig", "safe\n")
    _write(repo, "tracked~", "safe\n")
    _write(repo, "tracked.bak", "safe\n")
    _git(repo, "add", "-f", "tracked.orig", "tracked~", "tracked.bak")
    _write(repo, "candidate.rej", "safe\n")
    _write(repo, "candidate.swp", "safe\n")
    private_host = "ignored" + ".internal"
    _write(repo, "nested/ignored.orig", f"https://{private_host}/secret\n")
    _write(repo, "nested/ignored.swo", f"https://{private_host}/secret\n")

    findings = checker.scan_tracked(repo)
    backup_findings = {
        (finding.path, finding.digest)
        for finding in findings
        if finding.rule == "forbidden-backup-artifact"
    }

    assert backup_findings == {
        ("index:tracked.orig", checker._digest("tracked.orig")),
        ("index:tracked~", checker._digest("tracked~")),
        ("index:tracked.bak", checker._digest("tracked.bak")),
        ("untracked:candidate.rej", checker._digest("candidate.rej")),
        ("untracked:candidate.swp", checker._digest("candidate.swp")),
        ("ignored:nested/ignored.orig", checker._digest("nested/ignored.orig")),
        ("ignored:nested/ignored.swo", checker._digest("nested/ignored.swo")),
    }
    assert not any(
        finding.path.startswith("ignored:")
        and finding.rule == "url-private-or-local-host"
        for finding in findings
    )

    assert checker.main(["--root", str(repo)]) == 1
    output = capsys.readouterr().out
    assert "rule=forbidden-backup-artifact" in output
    for private_value in (
        "tracked.orig",
        "tracked~",
        "tracked.bak",
        "candidate.rej",
        "candidate.swp",
        "nested/ignored.orig",
        "nested/ignored.swo",
        private_host,
    ):
        assert private_value not in output
    assert "digest=" not in output and "path=" not in output


@pytest.mark.parametrize(
    "suffix",
    (".orig", ".rej", "~", ".bak", ".swp", ".swo"),
)
def test_history_scan_blocks_deleted_backup_artifact_paths(
    tmp_path: Path,
    suffix: str,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    removed_path = f"legacy/removed{suffix}"
    _write(repo, removed_path, "safe\n")
    _commit_all(repo)
    (repo / removed_path).unlink()
    _write(repo, "current.txt", "safe\n")
    _commit_all(repo)

    assert checker.scan_tracked(repo) == []
    findings = checker.scan_history(repo)

    assert any(
        finding.rule == "forbidden-backup-artifact"
        and finding.path == f"history-name:{removed_path}"
        for finding in findings
    )


def test_current_scan_fails_closed_when_ignored_backup_set_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checker = _load_checker()
    repo = _init_repo(tmp_path)
    _write(repo, ".gitignore", "*.orig\n")
    _write(repo, "tracked.txt", "safe\n")
    _commit_all(repo)
    real_backup_artifact_paths = checker._backup_artifact_paths
    created = False

    def changing_backup_paths(root: Path, *, ignored: bool) -> tuple[str, ...]:
        nonlocal created
        paths = real_backup_artifact_paths(root, ignored=ignored)
        if ignored and not created:
            _write(repo, "late.orig", "safe\n")
            created = True
        return paths

    monkeypatch.setattr(checker, "_backup_artifact_paths", changing_backup_paths)
    with pytest.raises(checker.PublicRepoCheckError, match="candidates changed"):
        checker.scan_tracked(repo)


def test_pypi_host_allowlist_is_exact() -> None:
    checker = _load_checker()
    assert checker.scan_text(
        "https://pypi.org/simple",
        path="candidate.txt",
    ) == []
    disallowed_subdomain = "private." + "pypi.org"
    findings = checker.scan_text(
        f"https://{disallowed_subdomain}/simple",
        path="candidate.txt",
    )
    assert {finding.rule for finding in findings} == {"url-host-not-allowlisted"}
