from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

from chatcopilot.core.install_artifacts import InstallError, download, extract_archive, write_private
from chatcopilot.plugins.repository import InstallRepository
from chatcopilot.plugins.runtime import UserServices, dependency_environment, require_free_port
from chatcopilot.plugins.service import PluginManager
from chatcopilot.plugins.types import PluginDefinition, ServiceDefinition


@pytest.fixture
def harness(tmp_path):
    calls = []
    running = set()
    entry = PluginDefinition("fixture", "fixture-plugin", "1.0.0", "fixture.driver", "3.12",
                             "An unrelated external program")

    class Services:
        def preflight(self):
            calls.append("preflight")
        def active(self, plugin, service):
            return service.name in running
        def register(self, plugin, services, root):
            calls.append("register")
        def start(self, plugin, services, root):
            calls.append("start")
            running.update(s.name for s in services)
        def stop(self, plugin, services, root):
            calls.append("stop")
            running.difference_update(s.name for s in services)
        def remove(self, plugin, services, root):
            self.stop(plugin, services, root)
            calls.append("remove")

    state = SimpleNamespace(fail=False)

    def bootstrap(entry, root, artifacts, uv):
        write_private(root / "runtime/program", "program")

    def artifacts(*args):
        wheels = tmp_path / "host.whl", tmp_path / "fixture.whl"
        for path in wheels:
            path.write_bytes(b"fixture distribution")
        return *wheels, "https://example.com/source"

    def driver(entry, root, operation):
        calls.append(operation)
        if state.fail:
            raise InstallError("interrupted prepare")
        if operation == "prepare":
            write_private(root / "data/user-history", "preserve")
            write_private(root / "config/runtime.env", "FIXTURE=value\n")
            return {"url": "http://localhost:19600", "provenance": {"revision": "fixture"},
                    "services": [{"name": "server", "command": ["/usr/bin/true"],
                                  "cwd": str(root), "environment_file": str(root / "config/runtime.env"),
                                  "port": 19600}]}
        return {"ok": True, "checks": {"fixture": True}}

    manager = PluginManager(tmp_path / "plugins", {entry.id: entry}, services=Services(),
        driver=driver, bootstrap=bootstrap, artifacts=artifacts,
        toolchain=lambda root: Path("/bin/uv"), progress=lambda _: None)
    return SimpleNamespace(manager=manager, entry=entry, calls=calls, running=running,
                           state=state, root=tmp_path / "plugins/fixture")


def test_generic_install_is_idempotent_and_uninstall_retains_data(harness):
    h = harness
    assert h.manager.install("fixture", options={"custom": "value"}, host={})["ok"]
    assert h.running == {"server"}
    request = (h.root / "config/request.json").read_bytes()
    assert h.manager.install("fixture", options={"custom": "ignored"}, host={})["ok"]
    assert h.calls.count("prepare") == 1
    assert (h.root / "config/request.json").read_bytes() == request
    h.manager.uninstall("fixture")
    assert not h.running and not (h.root / "runtime").exists()
    assert (h.root / "data/user-history").read_text() == "preserve"
    assert (h.root / "config/request.json").read_bytes() == request
    assert h.manager.list()[0]["status"] == "uninstalled"


def test_failed_install_records_stage_and_retries(harness):
    h = harness
    h.state.fail = True
    with pytest.raises(InstallError):
        h.manager.install("fixture", options={}, host={})
    record = InstallRepository(h.manager.root, "fixture").read()
    assert record["status"] == "failed" and record["stage"] == "prepare"
    assert "register" not in h.calls and not h.running
    h.state.fail = False
    assert h.manager.install("fixture", options={}, host={})["ok"]


def test_local_bundle_retry_retains_source_reference(harness):
    h = harness
    original = h.manager.artifacts
    h.manager.artifacts = lambda *args: (*original(*args)[:2], "")
    h.state.fail = True
    with pytest.raises(InstallError, match="interrupted"):
        h.manager.install("fixture", options={}, host={"source_url": "https://example.com/candidate"})
    h.state.fail = False
    assert h.manager.install("fixture", options={}, host={})["ok"]
    request = json.loads((h.root / "config/request.json").read_text())
    assert request["host"]["source_url"] == "https://example.com/candidate"


def test_version_change_requires_stop_and_replaces_only_program(harness):
    h = harness
    with pytest.raises(InstallError, match="Supported"):
        h.manager.install("fixture", options={}, host={}, selected_version="9.9.9")
    h.manager.install("fixture", options={}, host={})
    h.manager.definitions["fixture"] = replace(h.entry, version="2.0.0")
    with pytest.raises(InstallError, match="Stop"):
        h.manager.install("fixture", options={}, host={})
    h.manager.stop("fixture")
    (h.root / "runtime/old-program").write_text("obsolete")
    h.manager.install("fixture", options={}, host={})
    assert not (h.root / "runtime/old-program").exists()
    assert (h.root / "data/user-history").exists()
    assert h.manager.list()[0]["installed_version"] == "2.0.0"


def test_doctor_failure_stops_new_services(harness):
    h = harness
    original = h.manager.driver
    h.manager.driver = lambda entry, root, op: (
        {"ok": False, "checks": {"connection": False}} if op == "doctor" else original(entry, root, op))
    with pytest.raises(InstallError, match="connection check"):
        h.manager.install("fixture", options={}, host={})
    assert not h.running
    assert InstallRepository(h.manager.root, "fixture").read()["status"] == "failed"


def test_stop_before_install_does_not_prevent_install(harness):
    harness.manager.stop("fixture")
    assert harness.manager.install("fixture", options={}, host={})["ok"]


def test_private_record_and_lock_boundaries(tmp_path):
    repository = InstallRepository(tmp_path, "fixture")
    with repository.lock():
        with pytest.raises(InstallError, match="Another"):
            with repository.lock():
                pass
        repository.save({"version": "1", "status": "ready"})
    repository.path.chmod(0o644)
    with pytest.raises(InstallError, match="private"):
        repository.read()
    target = tmp_path / "external"
    target.mkdir()
    other = tmp_path / "other"
    other.symlink_to(target, target_is_directory=True)
    with pytest.raises(InstallError):
        with InstallRepository(tmp_path, "other").lock():
            pass


def test_corrupt_download_never_replaces_verified_cache(monkeypatch, tmp_path):
    import hashlib
    from chatcopilot.core import install_artifacts
    expected = hashlib.sha256(b"expected").hexdigest()
    monkeypatch.setattr(install_artifacts, "urlopen", lambda *a, **kw: io.BytesIO(b"corrupt"))
    with pytest.raises(InstallError, match="SHA256"):
        download("https://example.com/archive", expected, tmp_path / "cache")
    assert not list((tmp_path / "cache").iterdir())
    with pytest.raises(InstallError, match="credential-free"):
        download("https://" + "user:password@" + "example.com/archive", expected, tmp_path / "cache")


@pytest.mark.parametrize("name,kind", [("root/../../outside", "file"), ("root/link", "link")])
def test_archive_rejects_escape_and_links(tmp_path, name, kind):
    archive = tmp_path / "input.tar"
    with tarfile.open(archive, "w") as target:
        entry = tarfile.TarInfo(name)
        entry.size = 1
        if kind == "link":
            entry.type, entry.linkname = tarfile.SYMTYPE, "/tmp"
        target.addfile(entry, io.BytesIO(b"x") if kind == "file" else None)
    with pytest.raises(InstallError):
        extract_archive(archive, tmp_path / "extract")
    assert not (tmp_path / "outside").exists()


def test_port_conflict_and_private_service_ownership(tmp_path, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        with pytest.raises(InstallError, match=str(port)):
            require_free_port(port)
    service = ServiceDefinition("server", ("/usr/bin/true",), str(tmp_path),
                                str(tmp_path / "runtime.env"), 19600)
    manager = UserServices(tmp_path / "units")
    manager.directory.mkdir()
    path = manager.directory / manager.name("fixture", service)
    path.write_text("# someone else's service\n")
    commands = []
    monkeypatch.setattr(manager, "command", lambda *args: (
        commands.append(args) or subprocess.CompletedProcess(args, 0, "", "")))
    with pytest.raises(InstallError, match="another installation"):
        manager.remove("fixture", [service], tmp_path)
    assert not commands and path.exists()


def test_builders_do_not_receive_model_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setenv("AGENTSTRATA_AVATAR_CONTROL_TOKEN", "fixture-secret")
    monkeypatch.setenv("PYTHONPATH", "/tmp/untrusted")
    env = dependency_environment()
    assert not {"OPENAI_API_KEY", "AGENTSTRATA_AVATAR_CONTROL_TOKEN", "PYTHONPATH"} & env.keys()


def test_start_failure_stops_the_failed_unit(tmp_path, monkeypatch):
    service = ServiceDefinition("server", ("/usr/bin/true",), str(tmp_path),
                                str(tmp_path / "runtime.env"), 19600)
    manager = UserServices(tmp_path / "units")
    commands = []
    monkeypatch.setattr(manager, "_owned", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "active", lambda *args: False)
    monkeypatch.setattr("chatcopilot.plugins.runtime.require_free_port", lambda _: None)

    def command(*args):
        commands.append(args)
        return subprocess.CompletedProcess(args, 1 if args[0] == "start" else 0, "loaded", "")

    monkeypatch.setattr(manager, "command", command)
    with pytest.raises(InstallError, match="failed to start"):
        manager.start("fixture", [service], tmp_path)
    assert ("stop", manager.name("fixture", service)) in commands


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd parser is optional outside Linux hosts")
def test_generated_unit_is_accepted_by_real_systemd_parser(tmp_path, monkeypatch):
    root = tmp_path / "installation with spaces"
    root.mkdir()
    env = root / "private.env"
    env.write_text("FIXTURE=value\n")
    service = ServiceDefinition("server", ("/usr/bin/true",), str(root), str(env), 19600)
    manager = UserServices(tmp_path / "units")
    monkeypatch.setattr(manager, "command", lambda *args:
        subprocess.CompletedProcess(args, 0, "not-found", ""))
    manager.register("fixture", [service], root)
    path = manager.directory / manager.name("fixture", service)
    result = subprocess.run(["systemd-analyze", "--user", "verify", str(path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "ignoring" not in result.stderr.lower(), result.stderr


def test_service_definition_rejects_external_state(tmp_path):
    with pytest.raises(InstallError, match="inside"):
        ServiceDefinition.parse({"name": "server", "command": ["/bin/true"], "cwd": "/",
                                 "environment_file": str(tmp_path / "env"), "port": 19600}, root=tmp_path)


def test_cli_passes_only_explicit_software_options(harness, monkeypatch, capsys):
    from chatcopilot.plugins import cli
    monkeypatch.setattr(cli, "PLUGINS", harness.manager.definitions)
    monkeypatch.setattr(cli, "create_manager", lambda _: harness.manager)
    assert cli.main(["install", "fixture", "--root", str(harness.manager.root),
                     "--option", "custom=42", "--no-start"]) == 0
    request = json.loads((harness.root / "config/request.json").read_text())
    assert request["options"] == {"custom": 42}
    assert json.loads(capsys.readouterr().out)["ok"]


def test_agent_install_does_not_copy_terminal_only_credentials(harness, monkeypatch, capsys):
    from chatcopilot.plugins import cli
    from tests.model_settings_fixture import test_model_document
    harness.manager.definitions["fixture"] = replace(harness.entry, requires_agent=True)
    monkeypatch.setattr(cli, "PLUGINS", harness.manager.definitions)
    monkeypatch.setattr(cli, "create_manager", lambda _: harness.manager)
    monkeypatch.setattr(cli, "read_settings", test_model_document)
    monkeypatch.setenv("CHATCOPILOT_CHAT_API_KEY", "terminal-only-fixture")
    assert cli.main(["install", "fixture", "--root", str(harness.manager.root),
                     "--model-binding", "chat"]) == 1
    assert not harness.root.exists()
    assert "env_file" in capsys.readouterr().err


def test_release_checksums_and_wheel_identity_both_required(tmp_path, monkeypatch):
    import hashlib
    from chatcopilot.plugins import runtime
    monkeypatch.setattr(runtime, "version", lambda _: "1.0.0")
    entry = PluginDefinition("fixture", "fixture-plugin", "2.0.0", "fixture.driver", "3.12", "fixture")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    names = ["agentstrata-1.0.0-py3-none-any.whl", entry.wheel]
    for name, package, version in zip(names, ("agentstrata", "fixture-plugin"), ("1.0.0", "9.9.9"), strict=True):
        with zipfile.ZipFile(bundle / name, "w") as archive:
            archive.writestr("fixture.dist-info/METADATA", f"Name: {package}\nVersion: {version}\n")

    def checksums():
        (bundle / "SHA256SUMS").write_text("".join(
            hashlib.sha256((bundle / n).read_bytes()).hexdigest() + "  " + n + "\n" for n in names))

    checksums()
    with pytest.raises(InstallError, match="identity"):
        runtime.release_artifacts(entry, tmp_path / "cache", bundle)
    (bundle / names[1]).write_bytes(b"corrupt archive")
    with pytest.raises(InstallError, match="checksum"):
        runtime.release_artifacts(entry, tmp_path / "cache", bundle)
