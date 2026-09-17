from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("documentation_changes_gate", ROOT / "scripts/check_repo.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
         "-C", str(root), *args], env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repository with spaces"
    root.mkdir()
    git(root, "init", "-q")
    (root / "src").mkdir()
    (root / "src/base.py").write_text("value = 1\n")
    (root / "src/delete.py").write_text("value = 2\n")
    (root / ".gitignore").write_text("ignored/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    return root


def test_local_staged_unstaged_untracked_and_index_stability(gate, repo):
    assert gate._documentation_changes(repo) == ()
    (repo / "src/base.py").write_text("value = 3\n")
    git(repo, "add", "src/base.py")
    (repo / "src/delete.py").unlink()
    (repo / "src/中文 空格\n换行.py").write_text("value = 4\n")
    (repo / "ignored").mkdir()
    (repo / "ignored/private.py").write_text("ignored\n")
    index = (repo / ".git/index").read_bytes()
    assert set(gate._documentation_changes(repo)) == {"src/base.py", "src/delete.py", "src/中文 空格\n换行.py"}
    assert (repo / ".git/index").read_bytes() == index


def test_renames_and_explicit_base_include_both_paths(gate, repo):
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "mv", "src/base.py", "src/改名 文件.py")
    assert set(gate._documentation_changes(repo)) == {"src/base.py", "src/改名 文件.py"}
    git(repo, "commit", "-qm", "rename")
    (repo / "src/new.py").write_text("value = 4\n")
    assert set(gate._documentation_changes(repo, base_ref=base, explicit=["src/caller.py"])) == {
        "src/base.py", "src/改名 文件.py", "src/new.py", "src/caller.py"}


@pytest.mark.parametrize("base", ["missing-ref", "--output=unexpected", "; touch SHOULD_NOT_EXIST"])
def test_invalid_baseline_is_an_error_not_no_changes(gate, repo, base):
    with pytest.raises(ValueError, match="Git query or base reference failed"):
        gate._documentation_changes(repo, base_ref=base)
    assert not (repo / "SHOULD_NOT_EXIST").exists()


@pytest.mark.parametrize("length", [40, 64])
def test_first_push_lists_current_tree(gate, repo, length):
    expected = {os.fsdecode(x) for x in git(repo, "ls-files", "-z").split(b"\0") if x}
    assert set(gate._documentation_changes(repo, base_ref="0" * length)) == expected


def test_unborn_checkout_is_not_an_unavailable_snapshot(gate, tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("new repository")
    assert gate._documentation_changes(tmp_path) == ("README.md",)


def test_snapshot_never_queries_ancestor_or_operator_git(gate, repo, monkeypatch):
    snapshot = repo / "snapshot"
    snapshot.mkdir()
    # Even a candidate carrying a .git pointer must use the supplied manifest diff.
    (snapshot / ".git").write_text(f"gitdir: {repo / '.git'}\n")
    forbidden = Mock(side_effect=AssertionError("unexpected Git query"))
    monkeypatch.setattr(gate.subprocess, "run", forbidden)
    assert gate._documentation_changes(snapshot, isolated=True) is None
    assert gate._documentation_changes(snapshot, isolated=True, explicit=[]) == ()
    assert gate._documentation_changes(snapshot, isolated=True, explicit=["src/x.py"]) == ("src/x.py",)
    with pytest.raises(ValueError, match="isolated candidate"):
        gate._documentation_changes(snapshot, isolated=True, base_ref="HEAD")
    (snapshot / ".git").unlink()
    assert gate._documentation_changes(snapshot) is None
    forbidden.assert_not_called()


def test_inherited_git_variables_cannot_redirect_collection(gate, repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "other.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "other-index"))
    assert gate._documentation_changes(repo) == ()
    assert not (tmp_path / "other-index").exists()


def test_git_failure_is_not_silently_skipped(gate, repo, monkeypatch):
    monkeypatch.setattr(gate.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 128, b"", b"failure")))
    with pytest.raises(ValueError, match="Git query"):
        gate._documentation_changes(repo)
    monkeypatch.setattr(gate.subprocess, "run", Mock(side_effect=FileNotFoundError("git")))
    with pytest.raises(ValueError, match="Git could not be started"):
        gate._documentation_changes(repo)


def test_runner_passes_confirmed_empty_context(gate, monkeypatch, capsys):
    monkeypatch.setattr(gate, "_documentation_changes", lambda *args, **kwargs: ())
    monkeypatch.setattr(gate, "_profiles", lambda: {"docs": (gate.Check("documentation", (sys.executable, "checker.py")),)})
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(gate.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["check_repo.py", "docs"])
    assert gate.main() == 0
    assert "--changes-known" in run.call_args.args[0]
    capsys.readouterr()


@pytest.mark.parametrize("base", ["", "1" * 40, "0" * 40, "refs/heads/topic", "; touch SHOULD_NOT_EXIST #"])
def test_local_cli_passes_literal_baseline_and_changed_paths(gate, tmp_path, monkeypatch, capfd, base):
    checker = tmp_path / "documentation checker.py"
    checker.write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    changed = "src/中文 空格; touch SHOULD_NOT_EXIST.py"
    collect = Mock(return_value=(changed,))
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "_documentation_changes", collect)
    monkeypatch.setattr(gate, "_profiles", lambda: {
        "docs": (gate.Check("documentation", (sys.executable, str(checker)), cwd=tmp_path),),
    })
    monkeypatch.setattr(sys, "argv", ["check_repo.py", "docs", *(["--docs-base", base] if base else [])])

    assert gate.main() == 0
    collect.assert_called_once_with(tmp_path, base_ref=base or None, explicit=None, isolated=False)
    arguments = next(line for line in capfd.readouterr().out.splitlines() if line.startswith("["))
    assert json.loads(arguments) == ["--changes-known", "--changed-path=" + changed]
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
