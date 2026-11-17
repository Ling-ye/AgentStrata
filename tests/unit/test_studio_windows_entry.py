"""Exercise the installer launcher with isolated commands, never systemd or downloads."""
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "studio/windows/bootstrap.sh"


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + text)
    path.chmod(0o700)


@pytest.fixture
def launcher(tmp_path):
    home = tmp_path / "home"
    state = tmp_path / "private-state"
    resources = tmp_path / "resources"
    resources.mkdir()
    digest = "a" * 64
    (resources / "framework.sha256").write_text(digest + "\n")
    source = state / "platform" / digest
    source.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    trace = tmp_path / "trace"
    executable(bin_dir / "systemctl", 'echo "systemctl $*" >> "$TRACE"\n'
               'if [[ "$*" == *"WorkingDirectory"* ]]; then echo "$RUNNING_SOURCE"; exit; fi\n'
               'if [[ "$*" == *"MainPID"* ]]; then echo "${SERVICE_PID-12345}"; exit; fi\n'
               '[[ "$RUNNING" == yes ]]\n')
    executable(bin_dir / "systemd-run", 'echo "systemd-run $*" >> "$TRACE"\n')
    executable(bin_dir / "curl", "exit 0\n")
    executable(bin_dir / "npm", 'echo "npm $*" >> "$TRACE"\n'
               'if [[ "$1" == run && "$2" == build ]]; then mkdir -p console/web/dist; touch console/web/dist/index.html; fi\n')
    executable(bin_dir / "sleep", "exit 0\n")
    executable(source / ".venv/bin/python", 'echo "python $*" >> "$TRACE"\n'
               'if [[ "$*" == *"url"* ]]; then\n'
               '  [[ "${URL_READY:-yes}" == yes ]] || exit 1\n'
               '  echo "http://127.0.0.1:18636/#login=synthetic"\n'
               'fi\n')
    executable(source / "deploy/wsl/install_wsl_env.sh", 'echo prepare >> "$TRACE"\n')
    executable(home / ".local/share/agentstrata/uv/0.12.5/bin/uv", "exit 0\n")
    executable(home / ".local/share/agentstrata/node/test/node", "exit 0\n")
    env = {**os.environ, "HOME": str(home), "AGENTSTRATA_STUDIO_HOME": str(state),
           "PATH": str(bin_dir) + ":" + os.environ["PATH"], "TRACE": str(trace),
           "RUNNING": "no", "RUNNING_SOURCE": str(source)}
    def run():
        return subprocess.run(["bash", str(BOOTSTRAP), "launch", str(resources)],
                              env=env, capture_output=True, text=True, timeout=10)
    return state, source, env, trace, run


def test_launcher_builds_frontend_and_preserves_private_home(launcher):
    state, source, _, trace, run = launcher
    result = run()
    assert result.returncode == 0, result.stderr
    assert "#login=synthetic" in result.stdout
    calls = trace.read_text()
    assert "npm run build --prefix console/web" in calls
    assert calls.index("npm run build") < calls.index("systemd-run")
    assert f"--setenv=AGENTSTRATA_STUDIO_HOME={state}" in calls
    assert f"-m studio serve --home {state}" in calls
    assert (source / "console/web/dist/index.html").is_file()
    assert (source / ".studio-ready").is_file()


def test_launcher_does_not_replace_active_version(launcher):
    _, _, env, trace, run = launcher
    env.update(RUNNING="yes", RUNNING_SOURCE="/previous/version")
    result = run()
    assert result.returncode != 0
    assert "Another Studio version" in result.stderr
    calls = trace.read_text()
    assert "prepare" not in calls
    assert "systemd-run" not in calls
    assert "stop" not in calls


def test_new_installer_uses_bundled_frontend_without_recompiling(launcher):
    _, source, _, trace, run = launcher
    frontend = source / "console/web/dist"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("bundled production page")
    (frontend / "assets").mkdir()
    (frontend / "assets/current.js").write_text("bundled current application")
    result = run()
    assert result.returncode == 0, result.stderr
    calls = trace.read_text()
    assert "prepare" in calls and "-m studio.install" in calls
    assert not any(line.startswith("npm ") for line in calls.splitlines())
    assert (frontend / "index.html").read_text() == "bundled production page"
    assert (frontend / "assets/current.js").read_text() == "bundled current application"
    assert (source / ".studio-ready").is_file()


@pytest.mark.parametrize("status", ["thinking", "starting", "developing", "verifying", "packaging", "console-thinking"])
def test_launcher_blocks_upgrade_with_recoverable_tasks(launcher, status):
    state, source, _, trace, run = launcher
    with sqlite3.connect(state / "studio.sqlite") as db:
        db.execute("CREATE TABLE messages(state TEXT)")
        db.execute("INSERT INTO messages VALUES (?)", (status,))
    result = run()
    assert result.returncode != 0
    assert "unfinished development/build tasks" in result.stderr
    assert not (source / ".studio-ready").exists()
    assert "prepare" not in trace.read_text()


def test_prepared_version_can_resume_pending_tasks(launcher):
    state, source, _, _, run = launcher
    (state / "framework-version").write_text("a" * 64 + "\n")
    (source / ".studio-ready").touch()
    (source / "console/web/dist").mkdir(parents=True)
    (source / "console/web/dist/index.html").touch()
    with sqlite3.connect(state / "studio.sqlite") as db:
        db.execute("CREATE TABLE messages(state TEXT)")
        db.execute("INSERT INTO messages VALUES ('developing')")
    result = run()
    assert result.returncode == 0, result.stderr


def test_active_legacy_frontend_is_not_rebuilt_in_place(launcher):
    _, source, env, trace, run = launcher
    (source / ".studio-ready").touch()
    env["RUNNING"] = "yes"
    result = run()
    assert result.returncode != 0
    assert "frontend prepared" in result.stderr
    assert "npm" not in trace.read_text()


def test_windows_installer_retains_identity_and_surfaces_errors():
    script = (ROOT / "studio/windows/start.ps1").read_text(encoding="utf-8-sig")
    assert "'-u','agentstrata-studio','--exec'" in script
    assert "RedirectStandardError=$true" in script
    assert "StandardError.ReadToEndAsync()" in script
    assert "AgentStrataStudioSetup" in script


def test_previously_prepared_different_version_cannot_take_pending_work(launcher):
    state, source, _, trace, run = launcher
    (source / ".studio-ready").touch()
    (state / "framework-version").write_text("b" * 64 + "\n")
    with sqlite3.connect(state / "studio.sqlite") as db:
        db.execute("CREATE TABLE messages(state TEXT)")
        db.execute("INSERT INTO messages VALUES ('developing')")
    result = run()
    assert result.returncode != 0
    assert "unfinished development/build tasks" in result.stderr
    assert "systemd-run" not in trace.read_text()
    assert (state / "framework-version").read_text().strip() == "b" * 64


def test_legacy_pending_tasks_without_source_identity_are_not_adopted(launcher):
    state, source, _, _, run = launcher
    (source / ".studio-ready").touch()
    with sqlite3.connect(state / "studio.sqlite") as db:
        db.execute("CREATE TABLE messages(state TEXT)")
        db.execute("INSERT INTO messages VALUES ('developing')")
    result = run()
    assert result.returncode != 0
    assert "unfinished development/build tasks" in result.stderr


@pytest.mark.parametrize("status", ["pending", "selecting", "importing", "recovery_required", "future-state"])
def test_upgrade_does_not_adopt_unfinished_imports(launcher, status):
    state, source, _, trace, run = launcher
    (source / ".studio-ready").touch()
    (state / "framework-version").write_text("b" * 64 + "\n")
    with sqlite3.connect(state / "studio.sqlite") as db:
        db.execute("CREATE TABLE messages(state TEXT)")
        db.execute("CREATE TABLE studio_imports(id TEXT PRIMARY KEY, state TEXT, selection TEXT, updated REAL)")
        db.execute("INSERT INTO studio_imports VALUES ('synthetic', ?, '/private/source', 1)", (status,))
    result = run()
    assert result.returncode != 0
    assert "cannot safely resume" in result.stderr
    assert "/private/source" not in result.stderr
    assert "systemd-run" not in trace.read_text()
    assert "prepare" not in trace.read_text()
    assert (state / "framework-version").read_text().strip() == "b" * 64
    with sqlite3.connect(state / "studio.sqlite") as db:
        assert db.execute("SELECT state FROM studio_imports").fetchone()[0] == status


@pytest.mark.parametrize("status", ["imported", "cancelled", "failed", None])
def test_upgrade_allows_terminal_imports_and_legacy_database(launcher, status):
    state, _, _, _, run = launcher
    with sqlite3.connect(state / "studio.sqlite") as db:
        db.execute("CREATE TABLE messages(state TEXT)")
        if status is not None:
            db.execute("CREATE TABLE studio_imports(state TEXT)")
            db.execute("INSERT INTO studio_imports VALUES (?)", (status,))
    result = run()
    assert result.returncode == 0, result.stderr


def test_same_version_import_recovery_is_left_to_workbench(launcher):
    state, _, _, _, run = launcher
    (state / "framework-version").write_text("a" * 64 + "\n")
    with sqlite3.connect(state / "studio.sqlite") as db:
        db.execute("CREATE TABLE messages(state TEXT)")
        db.execute("CREATE TABLE studio_imports(state TEXT)")
        db.execute("INSERT INTO studio_imports VALUES ('recovery_required')")
    result = run()
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(state / "studio.sqlite") as db:
        assert db.execute("SELECT state FROM studio_imports").fetchone()[0] == "recovery_required"


def test_unknown_import_schema_fails_closed(launcher):
    state, _, _, trace, run = launcher
    with sqlite3.connect(state / "studio.sqlite") as db:
        db.execute("CREATE TABLE messages(state TEXT)")
        db.execute("CREATE TABLE studio_imports(unrecognized TEXT)")
    result = run()
    assert result.returncode != 0
    assert "systemd-run" not in trace.read_text()


def test_unready_service_cannot_replace_version_marker_even_if_port_responds(launcher):
    state, _, env, _, run = launcher
    original = "b" * 64 + "\n"
    (state / "framework-version").write_text(original)
    env["URL_READY"] = "no"
    # curl in this fixture succeeds, as it would for an unrelated local service.
    result = run()
    assert result.returncode != 0
    assert "#login=" not in result.stdout
    assert (state / "framework-version").read_text() == original


@pytest.mark.parametrize("pid", ["0", "", "unknown"])
def test_missing_service_owner_does_not_probe_or_record_version(launcher, pid):
    state, _, env, trace, run = launcher
    original = "b" * 64 + "\n"
    (state / "framework-version").write_text(original)
    env["SERVICE_PID"] = pid
    result = run()
    assert result.returncode != 0
    assert "-m studio url" not in trace.read_text()
    assert (state / "framework-version").read_text() == original


def test_launcher_binds_readiness_to_current_service_pid(launcher):
    _, _, _, trace, run = launcher
    result = run()
    assert result.returncode == 0, result.stderr
    assert "--expected-pid 12345" in trace.read_text()


def test_active_version_with_missing_preparation_marker_is_not_modified(launcher):
    _, source, env, trace, run = launcher
    (source / "console/web/dist").mkdir(parents=True)
    (source / "console/web/dist/index.html").touch()
    env["RUNNING"] = "yes"
    result = run()
    assert result.returncode != 0
    assert "prepare" not in trace.read_text()
    assert "npm" not in trace.read_text()


@pytest.mark.parametrize("status", [None, "future-development-state", ""])
def test_upgrade_rejects_unknown_development_states_without_changing_data(launcher, status):
    state, _, _, trace, run = launcher
    original = "b" * 64 + "\n"
    (state / "framework-version").write_text(original)
    database = state / "studio.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE messages(state TEXT)")
        db.execute("INSERT INTO messages VALUES (?)", (status,))
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    result = run()
    assert result.returncode != 0
    assert "unrecognized development state" in result.stderr
    assert "future-development-state" not in result.stderr
    assert (state / "framework-version").read_text() == original
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    assert "systemd-run" not in trace.read_text()
    assert "prepare" not in trace.read_text()


@pytest.mark.parametrize("status", ["ready", "failed", "discuss", "configure", "applied", "new_project"])
def test_upgrade_preserves_supported_terminal_development_history(launcher, status):
    state, _, _, _, run = launcher
    with sqlite3.connect(state / "studio.sqlite") as db:
        db.execute("CREATE TABLE messages(state TEXT)")
        db.execute("INSERT INTO messages VALUES (?)", (status,))
    result = run()
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(state / "studio.sqlite") as db:
        assert db.execute("SELECT state FROM messages").fetchone()[0] == status
