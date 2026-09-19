"""Real nested Codex sandbox checks; all repositories and credentials are synthetic."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatcopilot.core.source_snapshot import git_output
from chatcopilot.harness import codex_adapter, codex_environment
from chatcopilot.harness.models import HarnessError, RepairOptions
from chatcopilot.harness.agent_types import AgentCall, Role


def git(root, *args):
    return subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root), *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "original"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / "src").mkdir()
    (root / "src/example.py").write_text("value = 1\n")
    (root / "src/chatcopilot/core").mkdir(parents=True)
    (root / "src/chatcopilot/core/probe.py").write_text("value = 1\n")
    (root / "tests").mkdir()
    (root / "tests/test_probe.py").write_text("def test_probe(): pass\n")
    (root / ".env.example").write_text("EXAMPLE=public\n")
    (root / "docs/reference").mkdir(parents=True)
    (root / "docs/README.md").write_text("ordinary docs\n")
    (root / "docs/reference/rules.md").write_text("frozen rules\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    return root


@pytest.fixture
def native_binary():
    path = os.environ.get("CHATCOPILOT_CODEX_BIN") or shutil.which("codex")
    if not path or not shutil.which("bwrap"):
        pytest.skip("local native Codex CLI and bubblewrap required; no model is called")
    binary = Path(path).resolve()
    with binary.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            pytest.skip("native Linux Codex executable required")
    return binary


def metadata_bytes(root):
    common = Path(git_output(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return {p.relative_to(common).as_posix(): p.read_bytes() for p in common.rglob("*") if p.is_file()}


def intercept_adapter(monkeypatch, binary, tmp_path):
    adapter = codex_adapter.CodexCoder()
    monkeypatch.setattr(adapter, "preflight", lambda: (binary, tmp_path / "auth"))

    @contextlib.contextmanager
    def lease(_auth, _role, runtime_home, **kwargs):
        runtime_home.mkdir(mode=0o700, exist_ok=True)
        (runtime_home / "auth.json").write_text("synthetic-credential")
        (runtime_home / "config.toml").write_text("")
        yield SimpleNamespace(generation=1)

    monkeypatch.setattr(codex_adapter, "credential_lease", lease)
    return adapter


@pytest.mark.parametrize("kind,stage", [("linked", "prepare"), ("linked", "run"),
    ("linked", "review"), ("linked", "main"), ("linked", "plan"), ("ordinary", "run"), ("detached", "run")])
@pytest.mark.parametrize("source_kind", ["robot_task", "code_health"])
def test_actual_adapter_nested_git_queries_and_write_denials(repository, tmp_path, monkeypatch, native_binary, kind, stage, source_kind):
    root = repository
    if kind == "linked":
        root = tmp_path / "candidate"
        git(repository, "worktree", "add", "-q", "-b", "candidate", str(root))
    elif kind == "detached":
        git(root, "checkout", "--detach", "-q")
    (root / "src/example.py").write_text("value = 2\n")
    before = metadata_bytes(root)
    head = git_output(root, "rev-parse", "HEAD")
    evidence = {"source": {"kind": source_kind, "baseline_root": str(root),
        "repository_context": {"base_commit": head, "directory_kind": "git_worktree"}}}
    adapter = intercept_adapter(monkeypatch, native_binary, tmp_path)
    output = tmp_path / "execution"
    output.mkdir(mode=0o700)
    draft = output / "draft" if stage == "prepare" else None
    if draft:
        draft.mkdir(mode=0o700)
    role_name = {"prepare": "test", "run": "coding", "review": "review", "main": "main", "plan": "plan"}[stage]
    seen = {}
    real_permissions = codex_adapter.permission_config

    def permissions(scope, **kwargs):
        seen["scope"] = scope
        value = real_permissions(scope, **kwargs)
        if kwargs.get("private_paths"):
            seen["config"] = value
        return value

    monkeypatch.setattr(codex_adapter, "permission_config", permissions)
    queries = [["rev-parse", "HEAD"], ["status", "--short", "--branch"], ["diff", "HEAD"], ["log", "-1", "--format=%H:%s"]]
    expected = [git(root, *query) for query in queries]
    writes = [["add", "src/example.py"], ["commit", "--allow-empty", "-m", "denied"],
              ["update-ref", "refs/heads/forbidden", "HEAD"], ["config", "--local", "test.denied", "true"]]
    script = "\n".join([
        "import json, os, shutil, subprocess",
        f"prefix = {['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', str(root)]!r}",
        f"reads = [subprocess.run(prefix + q, capture_output=True, text=True, check=True).stdout for q in {queries!r}]",
        f"denied = [subprocess.run(prefix + q, capture_output=True).returncode for q in {writes!r}]",
        "assert os.environ.get('GIT_OPTIONAL_LOCKS') == '0'",
        "assert not any(name in os.environ for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'))",
        "rg = shutil.which('rg')",
        "search = subprocess.run(['rg', 'value', 'src/example.py'], cwd=prefix[-1], capture_output=True, text=True) if rg else None",
        f"private_visible = os.access({str(root.parent / 'sessions' / role_name / 'auth.json')!r}, os.R_OK)",
        f"original_visible = os.path.exists({str(repository / 'src/example.py')!r})",
        "from pathlib import Path",
        "def write_probe(name):",
        "    try:",
        "        p=Path(name); p.parent.mkdir(parents=True, exist_ok=True); p.write_text('candidate write'); return True",
        "    except OSError: return False",
        f"role_writes=[write_probe(p) for p in {[str(root/'src/chatcopilot/core/probe.py'), str(root/'tests/test_probe.py'), str(output/'draft/probe.py'), str(root/'docs/README.md'), str(root/'docs/reference/rules.md'), str(root/'docs/reference/new.md'), str(root/'.env.example'), str(root/'unexpected-root.py'), str(root/'.codex/config.toml')]!r}]",
        "print(json.dumps(dict(reads=reads, denied=denied, rg=rg, role_writes=role_writes, search=search.stdout if search else None, private_visible=private_visible, original_visible=original_visible)))",
    ])

    def process(outer, **kwargs):
        kwargs["cwd"] = kwargs["root"]
        kwargs["env"] = kwargs["environment"]
        native = [str(native_binary), "sandbox", "-P", "agentstrata", "-C", str(kwargs["cwd"])]
        for entry in seen["config"]:
            native += ["-c", entry]
        native += ["--", "/bin/bash", "-lc", shlex.join([str(Path(sys.executable).parent.resolve() / Path(sys.executable).name), "-c", script])]
        command = outer[:outer.index("--") + 1] + native
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, env=kwargs["env"])
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["reads"] == expected
        assert all(report["denied"]), report
        assert not report["private_visible"]
        assert report["role_writes"][:2] == [stage == "run", False]
        gc_write = stage == "run" and source_kind == "code_health"
        assert report["role_writes"][3:] == [gc_write, False, False, gc_write, False, False]
        assert (root / "docs/reference/rules.md").read_text() == "frozen rules\n"
        assert not (root / "docs/reference/new.md").exists()
        assert not (root / "unexpected-root.py").exists()
        assert not (root / ".codex/config.toml").exists()
        # Unmounted /tmp paths may be writable only in the private namespace;
        # authority concerns changes to host files, not disposable scratch data.
        assert (root / "src/chatcopilot/core/probe.py").read_text() == ("candidate write" if stage == "run" else "value = 1\n")
        assert (root / "tests/test_probe.py").read_text() == "def test_probe(): pass\n"
        assert (output / "draft/probe.py").exists() is (stage == "prepare")
        if kind == "linked":
            assert not report["original_visible"]
        if shutil.which("rg"):
            assert report["rg"] == "/sandbox-tools/rg"
            assert report["search"] == "value = 2\n"
        assert kwargs["cwd"] == root
        seen["model_boundary"] = True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex_adapter, "run_session", process)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.setenv(name, str(tmp_path / "foreign-git-state"))
    adapter._execute_impl(root, AgentCall("fixture", Role(role_name), 1, "fixture", {**evidence, "base_commit": head}), RepairOptions("unused"), output, lambda: None)
    assert seen["model_boundary"]
    assert metadata_bytes(root) == before
    for path in codex_environment.git_metadata(root):
        assert not seen["scope"].permits(path, write=True)


@pytest.mark.parametrize("broken", ["missing", "wrong_head", "unmounted"])
def test_environment_failure_prevents_model_start(repository, tmp_path, monkeypatch, native_binary, broken):
    root = tmp_path / "candidate"
    git(repository, "worktree", "add", "-q", "-b", "candidate", str(root))
    head = git_output(root, "rev-parse", "HEAD")
    if broken == "missing":
        (root / ".git").write_text("gitdir: /nonexistent/metadata\n")
    elif broken == "unmounted":
        monkeypatch.setattr(codex_adapter, "git_metadata", lambda _root: (tmp_path / "empty",))
        (tmp_path / "empty").mkdir()
    adapter = intercept_adapter(monkeypatch, native_binary, tmp_path)
    model = Mock()
    monkeypatch.setattr(codex_adapter, "run_session", model)
    with pytest.raises(HarnessError) as exc:
        adapter._execute_impl(root, AgentCall("fixture", Role.CODING, 1, "fixture", {"source": {}, "base_commit": "0" * 40 if broken == "wrong_head" else head}),
            RepairOptions("unused"), tmp_path / "output", lambda: None)
    assert exc.value.code == "coding_environment"
    model.assert_not_called()
