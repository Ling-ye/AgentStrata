"""Native package resolution and isolation regressions; no model calls."""
import json
import shutil
from types import SimpleNamespace

import pytest

from chatcopilot.core.codex_executable import resolve_codex_executable


@pytest.fixture(autouse=True)
def linux_x64(monkeypatch):
    monkeypatch.setattr("chatcopilot.core.codex_executable.platform.system", lambda: "Linux")
    monkeypatch.setattr("chatcopilot.core.codex_executable.platform.machine", lambda: "x86_64")


def installation(tmp_path, layout="nested", arch="x64"):
    root = tmp_path / "lib/node_modules/@openai/codex"
    entry = root / "bin/codex.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env node\nthrow new Error('must not execute launcher');\n")
    entry.chmod(0o755)
    dependency = "@openai/codex-linux-" + arch
    (root / "package.json").write_text(json.dumps({
        "name": "@openai/codex", "bin": {"codex": "bin/codex.js"},
        "optionalDependencies": {dependency: "npm:@openai/codex@1.0.0-linux-" + arch},
    }))
    payload_root = (root if layout == "vendor" else
                    tmp_path / "lib/node_modules" / dependency if layout == "hoisted" else
                    root / "node_modules" / dependency)
    payload_root.mkdir(parents=True, exist_ok=True)
    if layout != "vendor":
        (payload_root / "package.json").write_text(json.dumps({
            "name": "@openai/codex", "version": "1.0.0-linux-" + arch,
            "os": ["linux"], "cpu": [arch],
        }))
    triple = ("x86_64" if arch == "x64" else "aarch64") + "-unknown-linux-musl"
    binary = payload_root / "vendor" / triple / "bin/codex"
    binary.parent.mkdir(parents=True)
    shutil.copyfile("/bin/true", binary)
    binary.chmod(0o755)
    shim = tmp_path / "codex"
    shim.symlink_to(entry)
    return shim, binary, root, payload_root


@pytest.mark.parametrize("layout", ["nested", "hoisted", "vendor"])
@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_official_layouts_and_symlink_entry(tmp_path, monkeypatch, layout, arch):
    monkeypatch.setattr("chatcopilot.core.codex_executable.platform.machine",
                        lambda: "aarch64" if arch == "arm64" else "x86_64")
    entry, binary, _, _ = installation(tmp_path, layout, arch)
    assert resolve_codex_executable(entry) == binary
    assert resolve_codex_executable(binary) == binary


def test_package_directory_symlink_supports_pnpm(tmp_path):
    entry, binary, _, package = installation(tmp_path)
    relocated = tmp_path / "store/package"
    relocated.parent.mkdir()
    package.rename(relocated)
    package.symlink_to(relocated, target_is_directory=True)
    assert resolve_codex_executable(entry) == relocated / binary.relative_to(package)


@pytest.mark.parametrize("fault", ["missing", "script", "nonexec", "escape", "wrong_name", "wrong_arch", "metadata_escape"])
def test_invalid_payload_never_falls_back(tmp_path, fault):
    entry, binary, _, package = installation(tmp_path)
    if fault == "missing":
        binary.unlink()
    elif fault == "script":
        binary.write_text("#!/bin/sh\nexit 0\n")
    elif fault == "nonexec":
        binary.chmod(0o600)
    elif fault == "escape":
        binary.unlink()
        binary.symlink_to("/bin/true")
    elif fault == "metadata_escape":
        metadata = package / "package.json"
        target = tmp_path / "outside.json"
        metadata.rename(target)
        metadata.symlink_to(target)
    else:
        metadata = package / "package.json"
        value = json.loads(metadata.read_text())
        value["name" if fault == "wrong_name" else "cpu"] = "other" if fault == "wrong_name" else ["arm64"]
        metadata.write_text(json.dumps(value))
    with pytest.raises((ValueError, OSError)):
        resolve_codex_executable(entry)


@pytest.mark.parametrize("fault", ["name", "bin", "malformed", "platform", "architecture", "missing_dependency"])
def test_invalid_launcher_or_platform(tmp_path, monkeypatch, fault):
    entry, _, root, package = installation(tmp_path)
    if fault in {"platform", "architecture"}:
        monkeypatch.setattr("chatcopilot.core.codex_executable.platform." +
                            ("system" if fault == "platform" else "machine"), lambda: "unsupported")
    elif fault == "missing_dependency":
        shutil.rmtree(package)
    else:
        metadata = root / "package.json"
        value = json.loads(metadata.read_text())
        value[fault] = "invalid"
        metadata.write_text("{" if fault == "malformed" else json.dumps(value))
    with pytest.raises(ValueError):
        resolve_codex_executable(entry)


def test_unknown_wrapper_is_not_executed(tmp_path):
    marker = tmp_path / "executed"
    wrapper = tmp_path / "codex"
    wrapper.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
    wrapper.chmod(0o755)
    with pytest.raises(ValueError):
        resolve_codex_executable(wrapper)
    assert not marker.exists()


def test_runtime_mounts_only_resolved_payload(tmp_path, monkeypatch):
    from chatcopilot.agent.runtimes.codex import CodexRuntimeAdapter
    from chatcopilot.contracts.execution_scope import ExecutionScope

    entry, binary, root, _ = installation(tmp_path)
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    state = SimpleNamespace(workdir=work, codex_home=home, extension_env={},
                            execution_scope=ExecutionScope(readable_roots=(work,)))
    monkeypatch.setattr("chatcopilot.core.scoped_process.sandbox_command",
                        lambda *args, **kwargs: ["bwrap", "--", *args[0]])
    command = CodexRuntimeAdapter._wrap_isolated_command(state, [str(entry), "--version"])
    assert str(binary) in command
    assert str(entry) not in command and str(root) not in command
    assert command[-2:] == ["/opt/chatcopilot-codex/codex", "--version"]


@pytest.mark.parametrize("auth", [{"mode": "api_key", "key_env": "KEY"}, {"mode": "chatgpt", "profile": "test"}])
def test_catalog_identity_tracks_native_payload(tmp_path, auth):
    from chatcopilot.external_tools.codex_cli.catalog import connection_identity

    entry, binary, _, _ = installation(tmp_path)
    connection = {"kind": "codex", "auth": auth}
    env = {"CHATCOPILOT_CODEX_BIN": str(entry), "KEY": "synthetic"}
    before = connection_identity(connection, env)
    with binary.open("ab") as stream:
        stream.write(b"changed")
    assert connection_identity(connection, env) != before


def test_metadata_fifo_fails_without_waiting(tmp_path):
    import os
    entry, _, root, _ = installation(tmp_path)
    metadata = root / "package.json"
    metadata.unlink()
    os.mkfifo(metadata)
    with pytest.raises(ValueError):
        resolve_codex_executable(entry)


def test_release_digest_binds_payload_not_launcher(tmp_path, monkeypatch):
    import hashlib
    from chatcopilot.core import release_identity

    entry, binary, _, _ = installation(tmp_path)
    config = SimpleNamespace(codex_bin=str(entry))
    profile = SimpleNamespace(raw={"agents": {"runtime": "codex"}})
    monkeypatch.setattr(release_identity, "load_application_spec",
                        lambda _: SimpleNamespace(agent_profile=profile))
    monkeypatch.setattr("chatcopilot.core.model_settings.read_settings", lambda _: {})
    monkeypatch.setattr("chatcopilot.core.model_settings.resolve_binding", lambda *a, **kw: config)
    monkeypatch.setattr(release_identity, "environment_identity", lambda: {})
    monkeypatch.setattr(release_identity, "selected_model_identity", lambda *a: {})
    result = release_identity.execution_identity(tmp_path, {})
    assert result["environment"]["codex_executable_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert result["environment"]["codex_executable_sha256"] != hashlib.sha256(entry.read_bytes()).hexdigest()


def test_harness_preflight_returns_same_native_identity(tmp_path, monkeypatch):
    from chatcopilot.harness.codex_adapter import CodexCoder
    entry, binary, _, _ = installation(tmp_path)
    monkeypatch.setattr("chatcopilot.harness.codex_adapter.require_bubblewrap", lambda: None)
    monkeypatch.setattr("chatcopilot.harness.codex_adapter.validate_auth_root_path", lambda _: tmp_path)
    assert CodexCoder().preflight(SimpleNamespace(codex_bin=str(entry), credential_root="synthetic")) == (binary, tmp_path)


@pytest.mark.parametrize("name", [[], {}])
def test_non_string_platform_name_is_a_validation_error(tmp_path, name):
    entry, _, _, package = installation(tmp_path)
    metadata = package / "package.json"
    value = json.loads(metadata.read_text())
    value["name"] = name
    metadata.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Invalid Codex platform package metadata"):
        resolve_codex_executable(entry)
