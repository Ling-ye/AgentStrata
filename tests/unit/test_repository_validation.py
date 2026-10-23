from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_validation_profiles_include_static_and_runtime_checks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    check_repo = _load_script("check_repo.py")
    profiles = check_repo._profiles()
    fast_names = [check.name for check in profiles["fast"]]
    full_names = [check.name for check in profiles["full"]]
    assert fast_names == [
        "SDD metadata",
        "public repository boundary",
        "documentation",
        "architecture boundaries",
        "requirements drift",
        "UTF-8 source normalization",
        "Ruff",
        "typed contracts",
        "component catalog",
        "core tests",
    ]
    assert full_names[-5:] == [
        "installed dependency consistency",
        "Python wheel build smoke",
        "full Python tests",
        "console tests",
        "console production build",
    ]
    assert profiles["full"] == (*profiles["python"], *profiles["console"])
    assert not any("npm" in c.argv[0] for c in profiles["python"])
    assert [c.name for c in profiles["docs"]] == [
        "SDD metadata", "public repository boundary", "documentation", "diff format",
    ]
    assert all("pytest" not in c.argv and "build" not in c.argv for c in profiles["docs"])
    fast_pytest = profiles["fast"][-1]
    full_pytest = profiles["python"][-1]
    fast_paths = check_repo._fast_test_paths()
    assert fast_pytest.argv[3:3 + len(fast_paths)] == fast_paths
    assert fast_pytest.argv[3 + len(fast_paths):-1] == (
        "-q", "-n", "4", "--dist=loadfile",
    )
    assert "tests/unit" not in fast_pytest.argv
    assert full_pytest.argv[1:4] == ("-m", "pytest", "-q")
    assert full_pytest.argv[4:-1] == ("-n", "4", "--dist=loadfile")
    assert f"--basetemp={tmp_path / 'chatcopilot-pytest-fast'}" in fast_pytest.argv
    assert f"--basetemp={tmp_path / 'chatcopilot-pytest-full'}" in full_pytest.argv
    indexed_checks = {
        check.name
        for check in profiles["full"]
        if check.uses_repository_index
    }
    assert indexed_checks == {
        "public repository boundary",
        "UTF-8 source normalization",
        "Python wheel build smoke",
    }


def test_candidate_console_uses_the_snapshot_with_dependency_mounts(tmp_path):
    check_repo = _load_script("check_repo.py")
    profiles = check_repo._profiles(tmp_path)
    assert profiles["python"][-1].cwd == tmp_path
    assert all(check.cwd == ROOT / "console/web" for check in profiles["console"])
    assert all(check.argv[-3:] == ("--", "--configLoader", "runner")
               for check in profiles["console"])


def test_cli_logging_fixture_restores_process_state(monkeypatch):
    import io
    import logging
    from tests.conftest import isolated_runtime_logging
    from chatcopilot.core.log_context import current_log_context, push_log_context
    from chatcopilot.core.logging import configure_logging

    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    context = current_log_context()
    capture = io.StringIO()
    monkeypatch.setattr(sys, "stderr", capture)
    monkeypatch.delenv("CHATCOPILOT_LOG_DIR", raising=False)
    scope = isolated_runtime_logging.__wrapped__()
    next(scope)
    configure_logging("INFO")
    push_log_context(task_id="unfinished-fixture-task")
    assert any(getattr(handler, "_chatcopilot_stream_handler", False)
               for handler in root.handlers)
    with pytest.raises(StopIteration):
        next(scope)
    capture.close()
    assert root.handlers == before
    assert root.level == level
    assert current_log_context() == context


def test_fast_selection_preserves_file_order_and_ignores_comments(monkeypatch, tmp_path):
    check_repo = _load_script("check_repo.py")
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("test_first.py", "test_second.py"):
        (tests / name).write_text("", encoding="utf-8")
    (tests / "fast.txt").write_text(
        "# Daily core\n\n tests/test_second.py \n  # Boundary\ntests/test_first.py\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_repo, "ROOT", tmp_path)

    assert check_repo._fast_test_paths() == (
        "tests/test_second.py", "tests/test_first.py"
    )


@pytest.mark.parametrize("selection", (
    "# no selection\n",
    "tests/test_core.py\ntests/test_core.py\n",
    "tests/test_missing.py\n",
    "tests/../test_core.py\n",
    "tests/test_core.py::test_one\n",
    "--ignore=tests/test_core.py\n",
))
def test_fast_selection_rejects_invalid_manifest(monkeypatch, tmp_path, selection):
    check_repo = _load_script("check_repo.py")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text("", encoding="utf-8")
    (tests / "fast.txt").write_text(selection, encoding="utf-8")
    monkeypatch.setattr(check_repo, "ROOT", tmp_path)

    with pytest.raises(ValueError):
        check_repo._profiles()


def test_ci_public_boundary_preserves_history_scans_and_read_only_permissions():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"public-boundary", "python", "console", "vtuber-compatibility"}
    for job in workflow["jobs"].values():
        assert job.get("permissions", workflow["permissions"]) == {"contents": "read"}
        checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        assert checkout["with"]["persist-credentials"] is False
        assert job["runs-on"] == "ubuntu-24.04"
        assert "continue-on-error" not in job
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
            assert "continue-on-error" not in step
    job = workflow["jobs"]["public-boundary"]
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    assert [step["run"] for step in job["steps"] if "run" in step] == [
        "python scripts/check_public_repo.py --history", "bash scripts/check_secrets.sh history"]
    events = workflow.get("on", workflow.get(True))
    assert {"push", "pull_request", "workflow_dispatch"}.issubset(events)


def test_ci_console_runs_locked_install_tests_and_build():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["console"]
    assert [step["run"] for step in job["steps"] if "run" in step] == [
        "npm ci --prefix console/web",
        "python3 scripts/check_repo.py console --keep-going",
    ]


def test_ci_vtuber_uses_shared_script_without_credentials_or_path_filters():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["vtuber-compatibility"]
    assert job["name"] == "VTuber Python 3.12"
    assert next(s for s in job["steps"] if "setup-python@" in s.get("uses", ""))["with"]["python-version"] == "3.12"
    run_commands = {step.get("run") for step in job["steps"]}
    assert "python scripts/check_vtuber_compat.py" in run_commands
    assert "python scripts/check_live2d_generator.py" in run_commands
    assert all("if" not in s for s in job["steps"])
    assert "secrets." not in str(job)
    events = workflow.get("on", workflow.get(True))
    assert all("paths" not in str(event) for event in events.values())
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    release_commands = {step.get("run") for step in release["jobs"]["verify-vtuber"]["steps"]}
    assert {"python scripts/check_vtuber_compat.py", "python scripts/check_live2d_generator.py"} <= release_commands
    assert "verify-vtuber" in release["jobs"]["build"]["needs"]


def test_ci_python_checks_primary_runtime_with_locked_dependencies():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["python"]
    assert job["name"] == "Python 3.13"
    assert "strategy" not in job
    steps = job["steps"]
    assert next(s for s in steps if "setup-python@" in s.get("uses", ""))["with"]["python-version"] == "3.13"
    install = next(step["run"] for step in steps if step.get("run", "").startswith("uv sync "))
    assert "--python 3.13" in install
    assert "--locked" in install and "--frozen" not in install
    for extra in ("agent", "acp", "dev", "evaluation", "weixin"):
        assert f"--extra {extra}" in install
    assert "pip install" not in install
    assert any(step.get("run") == "bash scripts/setup_ci_linux.sh" for step in steps)
    gate = next(step for step in steps if "scripts/check_repo.py python" in step.get("run", ""))
    assert all(base in gate["env"]["DOCS_BASE"] for base in (
        "github.event.pull_request.base.sha", "github.event.before", "inputs.docs_base",
    ))
    assert '--docs-base "$DOCS_BASE"' in gate["run"]
    assert '--keep-going "${doc_args[@]}"' in gate["run"]


def test_release_verification_uses_the_same_locked_environment_and_full_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    runs = "\n".join(step.get("run", "") for step in workflow["jobs"]["verify"]["steps"])
    assert "uv sync --locked --python 3.13" in runs
    assert "--extra weixin" in runs
    assert "bash scripts/setup_ci_linux.sh" in runs
    assert ".venv/bin/python scripts/check_repo.py full --keep-going" in runs
    assert "pip install -e" not in runs
    assert "npm test" not in runs  # The full gate owns this check.
    for job_id in ("build", "install-test"):
        setup = next(step for step in workflow["jobs"][job_id]["steps"]
                     if "setup-python@" in step.get("uses", ""))
        assert setup["with"]["python-version"] == "3.12"


def test_ci_setup_refuses_to_modify_an_operator_host():
    import os
    import subprocess
    env = {key: value for key, value in os.environ.items()
           if key not in {"GITHUB_ACTIONS", "RUNNER_ENVIRONMENT"}}
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/setup_ci_linux.sh")],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "only for disposable GitHub-hosted runners" in result.stderr


def test_dependabot_updates_the_python_lockfile_ecosystem():
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    python = [update for update in config["updates"]
              if update["directory"] == "/" and update["package-ecosystem"] in {"pip", "uv"}]
    assert len(python) == 1
    assert python[0]["package-ecosystem"] == "uv"


def test_validation_subprocesses_use_one_wsl_temp_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    check_repo = _load_script("check_repo.py")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TEMP", "/mnt/c/Temp/example")
    monkeypatch.setenv("TMP", "/mnt/c/Temp/example")

    env = check_repo._check_env()

    assert env["TMPDIR"] == str(tmp_path)
    assert env["TEMP"] == str(tmp_path)
    assert env["TMP"] == str(tmp_path)


def test_validation_candidate_index_does_not_leak_into_test_subprocesses(
    monkeypatch,
    tmp_path: Path,
) -> None:
    check_repo = _load_script("check_repo.py")
    git_paths = {
        name: str(tmp_path / name.lower())
        for name in (
            "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"
        )
    }
    for name, value in git_paths.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")

    ordinary = check_repo._check_env()
    projected = check_repo._check_env(uses_repository_index=True)

    assert all(name not in ordinary for name in git_paths)
    assert "GIT_OPTIONAL_LOCKS" not in ordinary
    assert {name: projected[name] for name in git_paths} == git_paths
    assert projected["GIT_OPTIONAL_LOCKS"] == "0"


def test_build_smoke_detects_tracked_file_changes(tmp_path: Path) -> None:
    build_smoke = _load_script("build_smoke.py")
    tracked = tmp_path / "tracked.py"
    before = {tracked: None}
    after = {tracked: "changed"}
    assert build_smoke._changed_paths(before, after) == (tracked,)


def test_requirements_check_detects_dependency_missing_from_runtime_lock(monkeypatch, tmp_path):
    sync = _load_script("sync_requirements.py")
    monkeypatch.setattr(sync, "ROOT", tmp_path)
    monkeypatch.setattr(sync, "rendered_requirements", lambda: {})
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\n[project.optional-dependencies]\nagent=["deepeval==4.2.2"]\n')
    lock = ('[[package]]\nname="sample"\nsource={editable="."}\n'
            '[package.optional-dependencies]\nagent=[]\n')
    (tmp_path / "uv.lock").write_text(lock)
    assert sync.check() == ["uv.lock (agent dependency group; run uv lock)"]
    (tmp_path / "uv.lock").write_text(lock.replace('agent=[]', 'agent=[{name="deepeval"}]'))
    assert sync.check() == []


def test_gitleaks_wrapper_enforces_private_three_scope_scans() -> None:
    script = (ROOT / "scripts" / "check_secrets.sh").read_text(encoding="utf-8")

    assert "umask 077" in script
    assert "checkout-index --all" in script
    assert '"$candidate_root/index"' in script
    assert '"$candidate_root/worktree"' in script
    assert '"$candidate_root/untracked"' in script
    assert "--modified" in script
    assert "--others --exclude-standard" in script
    assert 'local ignore="$EMPTY_IGNORE"' in script
    assert 'if [[ "$scope" == history ]]' in script
    assert 'ignore="$HISTORY_IGNORE"' in script
    assert '--gitleaks-ignore-path "$ignore"' in script
    assert "--ignore-gitleaks-allow" in script
    assert "--max-decode-depth=2" in script
    assert "--report-format=json" in script
    assert '--report-path "$report"' in script
    assert '--exit-code=10' in script
    assert '> "$WORK_DIR/${scope}.log" 2>&1' in script
    assert "unsupported candidate path type: %s" not in script
    assert 'printf \'%s\\n\' "$relative_path"' not in script


def test_gitleaks_exceptions_are_exact_historical_fingerprints():
    fingerprints = [line for line in (ROOT / ".gitleaksignore").read_text().splitlines()
                    if line and not line.startswith("#")]
    assert fingerprints
    assert len(fingerprints) == len(set(fingerprints))
    assert all(re.fullmatch(r"[0-9a-f]{40}:[A-Za-z0-9_./-]+:[a-z0-9-]+:[1-9][0-9]*", f)
               for f in fingerprints)


@pytest.mark.parametrize(("scanner_exit", "expected_exit", "keep_report"), (
    (0, 0, False), (10, 1, False), (10, 1, True), (1, 2, False),
))
def test_secret_scan_failures_are_distinct_and_diagnostics_stay_private(
    tmp_path, monkeypatch, scanner_exit, expected_exit, keep_report,
):
    import json
    import os
    import shutil
    import subprocess

    tools = tmp_path / "tools"
    tools.mkdir()
    scanner = tmp_path / "scanner"
    scanner.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "report = pathlib.Path(sys.argv[sys.argv.index('--report-path') + 1])\n"
        "report.write_text(json.dumps([{'RuleID': 'fixture-rule', "
        "'Secret': 'never-print-this-value', 'File': 'private-fixture-path'}]))\n"
        "print('never-print-this-value')\n"
        f"sys.exit({scanner_exit})\n"
    )
    scanner.chmod(0o700)
    # Mock the download only; exercise the actual wrapper, status mapping,
    # report handling and cleanup without requiring network in unit tests.
    programs = {
        "curl": "exit 0\n",
        "sha256sum": "cat > /dev/null\nexit 0\n",
        "tar": 'cp "$SCANNER_TEST_BIN" "$4/gitleaks"\n',
    }
    for name, body in programs.items():
        path = tools / name
        path.write_text("#!/bin/bash\nset -e\n" + body)
        path.chmod(0o700)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SCANNER_TEST_BIN", str(scanner))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/check_secrets.sh"), "history",
         *(["--keep-report"] if keep_report else [])],
        capture_output=True, text=True,
    )
    assert result.returncode == expected_exit
    output = result.stdout + result.stderr
    assert "never-print-this-value" not in output
    assert "private-fixture-path" not in output
    assert ("Finding counts by rule" in output) == (scanner_exit == 10)
    assert ("ERROR:" in output) == (scanner_exit == 1)
    assert not list(tmp_path.glob("agentstrata-gitleaks.*"))
    if keep_report:
        match = re.search(r"Private diagnostics retained at (/tmp/[^;]+);", output)
        assert match
        directory = Path(match.group(1))
        try:
            assert directory.stat().st_mode & 0o777 == 0o700
            report = directory / "history.json"
            assert report.stat().st_mode & 0o777 == 0o600
            assert json.loads(report.read_text())[0]["RuleID"] == "fixture-rule"
        finally:
            shutil.rmtree(directory)
    else:
        assert "Private diagnostics retained" not in output


def test_gitleaks_policy_covers_network_and_query_leaks() -> None:
    config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")

    assert 'id = "agentstrata-private-host"' in config
    assert 'id = "agentstrata-private-ipv4"' in config
    assert 'id = "agentstrata-sensitive-uri-query"' in config
    assert 'description = "Standard WSL localhost bridge hostname"' in config
    assert 'regexTarget = "match"' in config
    assert r"wsl\.localhost" in config
    for marker in (
        ".corp",
        ".home",
        ".internal",
        ".intranet",
        ".lan",
        ".local",
        "access_token",
        "client_secret",
        "192.168.",
    ):
        assert marker in config

    def rule_regex(rule_id: str) -> re.Pattern[str]:
        rule = config.split(f'id = "{rule_id}"', maxsplit=1)[1]
        rule = rule.split("[[rules]]", maxsplit=1)[0]
        match = re.search(r"regex = '''(.+?)'''", rule, flags=re.DOTALL)
        assert match is not None
        return re.compile(match.group(1))

    private_host = rule_regex("agentstrata-private-host")
    private_suffix = ".".join(("private", "lan"))
    assert private_host.search(f"https://service.{private_suffix}/artifact")
    assert private_host.search("service." + "internal")
    assert private_host.search("127.example." + "local" + "/path")
    assert not private_host.search(".env.local")

    sensitive_query = rule_regex("agentstrata-sensitive-uri-query")
    api_key = "api" + "_key"
    token = "to" + "ken"
    fixture_value = "abcdefghijkl"
    assert sensitive_query.search(
        f"https://api.example.com/data?{api_key}={fixture_value}"
    )
    assert sensitive_query.search(
        f"https://api.example.com/data?page=1&{token}={fixture_value}"
    )
    assert not sensitive_query.search("api_key=fallback.api_key")


def test_release_runbook_preserves_signed_tag_and_draft_boundaries() -> None:
    runbook = (ROOT / "docs" / "guides" / "releasing.md").read_text(encoding="utf-8")

    assert "0.1.0.dev0" in runbook
    assert "git tag -s v0.1.0" in runbook
    assert "git push origin refs/tags/v0.1.0" in runbook
    assert "scripts/check_public_repo.py --history" in runbook
    assert "scripts/check_secrets.sh history" in runbook
    assert "draft GitHub Release" in runbook
    assert "不发布 PyPI" in runbook


def test_pytest_inventory_tracks_identities_and_skips(tmp_path: Path) -> None:
    gate = _load_script("check_repo.py")
    report = tmp_path / "tests.xml"
    report.write_text('<testsuites><testsuite><testcase classname="m" name="a"/><testcase classname="m" name="b"><skipped/></testcase></testsuite></testsuites>')
    first = gate._test_inventory(report)
    assert first['count'] == 2 and first['skipped_ids'] == ['m::b'] and first['error_ids'] == []
    report.write_text('<testsuites><testsuite><testcase classname="m" name="b"><error/></testcase><testcase classname="m" name="a"/></testsuite></testsuites>')
    second = gate._test_inventory(report)
    assert second['sha256'] == first['sha256'] and second['skipped_ids'] == [] and second['error_ids'] == ['m::b']


def test_repository_report_collects_real_pytest_inventory(tmp_path: Path, monkeypatch) -> None:
    import json
    import sys
    gate = _load_script('check_repo.py')
    test = tmp_path / 'test_inventory.py'
    test.write_text("""import pytest

def test_executes():
    assert 2 + 2 == 4

@pytest.mark.skip(reason="fixture")
def test_skipped():
    assert False
""")
    monkeypatch.setattr(gate, '_profiles', lambda: {'fast': (gate.Check('core tests', (sys.executable, '-m', 'pytest', str(test), '-q'), cwd=tmp_path),)})
    output = tmp_path / 'report'
    monkeypatch.setattr(sys, 'argv', ['check_repo.py', 'fast', '--report-dir', str(output)])
    assert gate.main() == 0
    record = json.loads((output / 'manifest.json').read_text())['checks'][0]
    assert record['test_inventory']['count'] == 2
    assert len(record['test_inventory']['skipped_ids']) == 1


def test_repository_report_keeps_failed_unittest_subtests(tmp_path: Path, monkeypatch) -> None:
    import json
    import sys
    gate = _load_script('check_repo.py')
    test = tmp_path / 'test_subtest.py'
    test.write_text("""import unittest

class TestBehavior(unittest.TestCase):
    def test_contract(self):
        for value in (1, 2):
            with self.subTest(value=value):
                self.assertEqual(value, 1)
""")
    monkeypatch.setattr(gate, '_profiles', lambda: {'fast': (gate.Check('core tests', (sys.executable, '-m', 'pytest', str(test), '-q'), cwd=tmp_path),)})
    output = tmp_path / 'report'
    monkeypatch.setattr(sys, 'argv', ['check_repo.py', 'fast', '--report-dir', str(output)])
    assert gate.main() == 1
    record = json.loads((output / 'manifest.json').read_text())['checks'][0]
    assert record['status'] == 'failed'
    assert record['failed_ids'] == ['test_subtest.py::TestBehavior::test_contract']
