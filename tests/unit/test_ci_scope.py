from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scope(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("ci_scope")


@pytest.mark.parametrize("paths,expected", [
    (("docs/guides/development.md", "specs/ci-validation/spec.md"), (False, False, False)),
    (("console/web/src/App.tsx",), (False, False, False)),
    (("tests/unit/test_code_task_runtime.py",), (False, False, False)),
    (("integrations/open_llm_vtuber/agentstrata_vtuber/resources/patches/frontend.patch",), (True, False, True)),
    (("tests/integration/test_vtuber_upstream_contract.py",), (True, False, True)),
    (("integrations/live2d_generator/agentstrata_live2d/install.py",), (False, True, True)),
    (("tests/unit/test_character_presets.py",), (False, True, True)),
    (("tests/unit/test_live2d_generation.py",), (False, True, True)),
    (("integrations/live2d_desktop/src/main.ts",), (False, False, True)),
    (("src/chatcopilot/plugins/cli.py",), (True, True, True)),
    (("uv.lock",), (True, True, True)),
    ((".github/workflows/ci.yml",), (True, True, True)),
    (("scripts/ci_scope.py",), (True, True, True)),
    (("tests/unit/test_plugin_artifacts.py",), (True, True, True)),
    (("tests/conftest.py",), (True, True, True)),
    (("integrations/future_plugin/main.py",), (True, True, True)),
    (("LICENSE",), (True, True, True)),
    ((), (False, False, False)),
])
def test_scope_matrix(scope, paths, expected):
    result = scope.select(paths, event="push")
    assert tuple(result[key] for key in scope.EXTENSIONS) == expected
    assert result["package"] is False


@pytest.mark.parametrize("base", [None, "", "0" * 40, "missing-reference"])
def test_missing_or_invalid_base_runs_all_extensions_without_packaging(scope, tmp_path, base):
    result = scope.repository_scope(tmp_path, event="push", base=base)
    assert all(result[key] for key in scope.EXTENSIONS)
    assert result["package"] is False and result["reason"] == "unknown_base"


def test_manual_full_overrides_empty_or_documentation_only_changes(scope):
    result = scope.select(("README.md",), event="workflow_dispatch")
    assert all(result[key] for key in (*scope.EXTENSIONS, "package"))
    assert result["reason"] == "manual_full"


def test_unknown_event_is_conservative(scope, tmp_path):
    assert scope.repository_scope(tmp_path, event="future-event", base="HEAD")["desktop"] is True


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "maintenance.auto", "false")
    git(root, "config", "user.name", "CI Fixture")
    git(root, "config", "user.email", "ci@example.invalid")
    plugin = root / "integrations/live2d_generator/example.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("original generator fixture\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    return root


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_deletion_remains_a_related_change(scope, repository, event):
    base = git(repository, "rev-parse", "HEAD")
    (repository / "integrations/live2d_generator/example.py").unlink()
    result = scope.repository_scope(repository, event=event, base=base)
    assert result["generator"] and result["desktop"] and not result["vtuber"]


def test_cross_directory_rename_keeps_both_old_and_new_module_scopes(scope, repository):
    base = git(repository, "rev-parse", "HEAD")
    new = repository / "integrations/open_llm_vtuber/example.py"
    new.parent.mkdir()
    git(repository, "mv", "integrations/live2d_generator/example.py", str(new))
    result = scope.repository_scope(repository, event="pull_request", base=base)
    assert all(result[key] for key in scope.EXTENSIONS)


def test_valid_clean_diff_is_not_an_unknown_base(scope, repository):
    result = scope.repository_scope(repository, event="push", base="HEAD")
    assert not any(result[key] for key in scope.EXTENSIONS)
    assert result["reason"] == "no_changes"


def test_cli_outputs_boolean_flags_and_a_constant_reason(scope, repository, tmp_path, monkeypatch, capsys):
    output, summary = tmp_path / "outputs", tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert scope.main(["--root", str(repository), "--event", "workflow_dispatch",
                       "--github-output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["package"] is True
    assert "package=true\n" in output.read_text() and "reason=manual_full\n" in output.read_text()
    assert "manual_full" in summary.read_text()


def test_standalone_script_imports_without_development_dependencies(tmp_path):
    result = subprocess.run([sys.executable, "-S", str(ROOT / "scripts/ci_scope.py"),
                             "--event", "workflow_dispatch", "--root", str(tmp_path)],
                            check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)["package"] is True
