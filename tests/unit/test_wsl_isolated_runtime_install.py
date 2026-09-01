from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "deploy/wsl/install_wsl_env.sh"
NODE_TOOLS = REPO_ROOT / "deploy/wsl/node-tools"
START_SCRIPT = REPO_ROOT / "deploy/wsl/start.sh"
LOAD_ENV_SCRIPT = REPO_ROOT / "deploy/wsl/_load_env.sh"
BOOTSTRAP_SCRIPT = REPO_ROOT / "deploy/wsl/bootstrap_wsl.sh"


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_dry_run(tmp_path: Path, *, architecture: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "home")
    environment["AGENTSTRATA_RUNTIME_ROOT"] = str(tmp_path / "runtime")
    (tmp_path / "home").mkdir()
    if architecture is not None:
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        uname = fake_bin / "uname"
        uname.write_text(f"#!/bin/sh\nprintf '%s\\n' {architecture}\n", encoding="utf-8")
        uname.chmod(0o755)
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    return subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--no-system-packages",
            "--dry-run",
            "--no-verify",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_installer_pins_isolated_minimal_runtimes() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    assert 'UV_VERSION="0.12.5"' in script
    assert 'PYTHON_VERSION="3.13.15"' in script
    assert 'NODE_VERSION="24.20.0"' in script
    assert "68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2" in script
    assert "9bf43b4d1a07665bf64d4c4e710930b382321a785e0eb10aac07f46471f86a31" in script
    assert "2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2" in script
    assert "5f4ddab610c1ab2016b3c227cebdbf6d9495161487e4739c7b90090595f465f7" in script
    assert "b65f23a420c4acc96427efb30e5ed9bc0f7e25d2d712000f6ede77c1a0de5f46" in script
    assert "92804e2f635c1791bb497437d94b15970f0d6d74811979315624cbb0f45b778d" in script
    assert "89af8424dd53e560b1933f87ba650d8bf57c83ca5a04600eefb31f416aabbae7" in script
    assert "23a5637c2470fde09fcc1acc77c1b92e04e3d7e3e6e80ff7df6f5831958d1477" in script
    assert 'sync --frozen --python "$PYTHON_VERSION" --extra agent --extra acp' in script
    assert 'python install --no-bin "$PYTHON_VERSION"' in script
    assert 'RUNTIME_ROOT="${AGENTSTRATA_RUNTIME_ROOT:-$HOME/.local/share/agentstrata}"' in script
    assert "--retry 5 --retry-all-errors" in script
    assert "--retry-delay 2 --retry-max-time 180" in script
    assert "npm install -g" not in script
    assert "npm config set prefix" not in script
    assert "nodesource.com" not in script.lower()
    assert ".bashrc" not in script
    assert "@larksuite/cli" not in script
    assert "[test]" not in script
    assert "requirements.txt" not in script
    assert "numpy" not in script.lower()
    assert "pandas" not in script.lower()
    assert "pyinstaller" not in script.lower()
    assert '"cc-connect v$CC_CONNECT_VERSION"' in script
    assert "NPM_CONFIG_USERCONFIG=/dev/null" in script
    assert "NPM_CONFIG_GLOBALCONFIG=/dev/null" in script
    assert "sync --frozen --python \"$PYTHON_VERSION\" --extra agent --extra acp --no-config" in script
    assert "GatewayAcpAgent" in script
    assert "GatewayAcpServer" not in script


def test_node_tool_lock_contains_only_pinned_cc_connect() -> None:
    package = json.loads((NODE_TOOLS / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((NODE_TOOLS / "package-lock.json").read_text(encoding="utf-8"))

    assert package["private"] is True
    assert package["dependencies"] == {"cc-connect": "1.4.0-beta.3"}
    assert package["allowScripts"] == {"cc-connect@1.4.0-beta.3": True}
    assert lock["lockfileVersion"] == 3
    assert lock["packages"]["node_modules/cc-connect"]["version"] == "1.4.0-beta.3"
    assert lock["packages"]["node_modules/cc-connect"]["integrity"].startswith("sha512-")


def test_dry_run_is_zero_write_and_reports_x86_artifacts(tmp_path: Path) -> None:
    completed = _run_dry_run(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / "runtime").exists()
    assert list((tmp_path / "home").iterdir()) == []
    assert "uv-x86_64-unknown-linux-gnu.tar.gz" in completed.stdout
    assert "node-v24.20.0-linux-x64.tar.xz" in completed.stdout
    assert "uv sync --frozen --python 3.13.15 --extra agent --extra acp" in completed.stdout
    assert "dry-run completed; no files or packages were changed" in completed.stdout


def test_dry_run_maps_aarch64_to_locked_artifacts(tmp_path: Path) -> None:
    completed = _run_dry_run(tmp_path, architecture="aarch64")

    assert completed.returncode == 0, completed.stderr
    assert "uv-aarch64-unknown-linux-gnu.tar.gz" in completed.stdout
    assert "node-v24.20.0-linux-arm64.tar.xz" in completed.stdout
    assert "9bf43b4d1a07665bf64d4c4e710930b382321a785e0eb10aac07f46471f86a31" in completed.stdout
    assert "5f4ddab610c1ab2016b3c227cebdbf6d9495161487e4739c7b90090595f465f7" in completed.stdout
    assert not (tmp_path / "runtime").exists()


def test_installer_scrubs_bot_and_model_secrets_before_child_commands(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    leak_marker = tmp_path / "secret-leaked"
    home.mkdir()
    _write_executable(
        fake_bin / "uname",
        "#!/usr/bin/env bash\n"
        "if [ -n \"${CHATCOPILOT_CHAT_API_KEY:-}\" ] "
        "|| [ -n \"${QQ_ACCESS_TOKEN:-}\" ] "
        "|| [ -n \"${SSH_AUTH_SOCK:-}\" ] "
        "|| [ -n \"${UV_INDEX_URL:-}\" ] "
        "|| [ -n \"${NPM_CONFIG_USERCONFIG:-}\" ] "
        "|| [ -n \"${CODEX_HOME:-}\" ]; then\n"
        "  exit 97\n"
        "fi\n"
        "printf '%s\\n' x86_64\n",
    )
    secret = "deployment-secret-must-not-leak"
    https_scheme = "https"
    completed = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--no-system-packages",
            "--dry-run",
            "--no-verify",
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "LEAK_MARKER": str(leak_marker),
            "CHATCOPILOT_CHAT_API_KEY": secret,
            "QQ_ACCESS_TOKEN": secret,
            "SSH_AUTH_SOCK": str(tmp_path / "agent.sock"),
            "UV_INDEX_URL": f"{https_scheme}://user:{secret}@packages.example.invalid/simple",
            "NPM_CONFIG_USERCONFIG": str(tmp_path / "secret-npmrc"),
            "CODEX_HOME": str(tmp_path / "codex-home"),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not leak_marker.exists()
    assert secret not in completed.stdout
    assert secret not in completed.stderr


def test_legacy_feishu_start_executes_cc_connect_with_private_node(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    deploy = runtime / "deploy" / "wsl"
    deploy.mkdir(parents=True)
    for source in (START_SCRIPT, LOAD_ENV_SCRIPT):
        (deploy / source.name).write_bytes(source.read_bytes())
    _write_executable(deploy / "_stop_cc.sh", "#!/usr/bin/env bash\nexit 0\n")
    bot_spec = runtime / "bots" / "test-bot" / "bot.yaml"
    bot_spec.parent.mkdir(parents=True)
    bot_spec.write_text("id: test-bot\nplatform:\n  type: feishu\n", encoding="utf-8")
    _write_executable(runtime / ".venv" / "bin" / "python", "#!/usr/bin/env bash\nexit 0\n")

    home = tmp_path / "home"
    cc_home = tmp_path / "cc-home"
    fake_bin = tmp_path / "bin"
    private_marker = tmp_path / "private-node-used"
    system_marker = tmp_path / "system-node-used"
    injection_marker = tmp_path / "node-options-used"
    node = (
        home
        / ".local/share/agentstrata/node/node-v24.20.0-linux-x64/bin/node"
    )
    cc_entry = (
        home
        / ".local/share/agentstrata/node-tools/cc-connect-1.4.0-beta.3"
        / "node_modules/cc-connect/run.js"
    )
    _write_executable(
        node,
        "#!/usr/bin/env bash\n"
        "if [ -n \"${NODE_OPTIONS:-}\" ] || [ -n \"${NODE_PATH:-}\" ]; then\n"
        "  : > \"$NODE_INJECTION_MARKER\"\n"
        "fi\n"
        "if [ \"${1:-}\" = --version ]; then printf '%s\\n' v24.20.0; exit 0; fi\n"
        ": > \"$PRIVATE_NODE_MARKER\"\n"
        "printf '%s\\n' \"$1\" > \"$PRIVATE_NODE_ARGUMENT\"\n",
    )
    _write_executable(cc_entry, "#!/usr/bin/env node\n")
    _write_executable(
        fake_bin / "sha256sum",
        "#!/usr/bin/env bash\n"
        f"if [ \"${{1:-}}\" = \"{node}\" ]; then\n"
        "  printf '%s  %s\\n' "
        "89af8424dd53e560b1933f87ba650d8bf57c83ca5a04600eefb31f416aabbae7 \"$1\"\n"
        "else\n"
        "  exec /usr/bin/sha256sum \"$@\"\n"
        "fi\n",
    )
    _write_executable(
        fake_bin / "node",
        "#!/usr/bin/env bash\n: > \"$SYSTEM_NODE_MARKER\"\nexit 99\n",
    )

    completed = subprocess.run(
        ["bash", str(deploy / "start.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "CHATCOPILOT_HOME": str(runtime),
            "CHATCOPILOT_INSTANCE_ID": "test-bot",
            "CHATCOPILOT_BOT_SPEC": str(bot_spec),
            "CHATCOPILOT_CC_HOME": str(cc_home),
            "CHATCOPILOT_CC_CONNECT_CONFIG_DIR": str(cc_home / ".cc-connect"),
            "CHATCOPILOT_CC_CONNECT_BIN": str(cc_entry),
            "CHATCOPILOT_LOG_DIR": str(tmp_path / "logs"),
            "PRIVATE_NODE_MARKER": str(private_marker),
            "PRIVATE_NODE_ARGUMENT": str(tmp_path / "private-node-argument"),
            "SYSTEM_NODE_MARKER": str(system_marker),
            "NODE_INJECTION_MARKER": str(injection_marker),
            "NODE_OPTIONS": "--require=/tmp/untrusted-preload.js",
            "NODE_PATH": "/tmp/untrusted-node-modules",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert private_marker.exists()
    assert not system_marker.exists()
    assert not injection_marker.exists()
    assert (tmp_path / "private-node-argument").read_text(encoding="utf-8").strip() == str(cc_entry)


def test_gateway_start_executes_python_host_without_node_or_cc_connect(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    deploy = runtime / "deploy" / "wsl"
    deploy.mkdir(parents=True)
    for source in (START_SCRIPT, LOAD_ENV_SCRIPT):
        (deploy / source.name).write_bytes(source.read_bytes())
    legacy_stop_marker = tmp_path / "legacy-stop-complete"
    _write_executable(
        deploy / "_stop_cc.sh",
        "#!/usr/bin/env bash\n"
        ': > "$LEGACY_STOP_MARKER"\n',
    )

    bot_spec = runtime / "bots" / "test-bot" / "bot.yaml"
    bot_spec.parent.mkdir(parents=True)
    bot_spec.write_text(
        "id: test-bot\ngateway:\n  protocol_version: 1\nchannels:\n  qq:\n    type: qq_personal\n",
        encoding="utf-8",
    )
    argv_file = tmp_path / "gateway-argv"
    env_file = tmp_path / "gateway-env"
    _write_executable(
        runtime / ".venv" / "bin" / "python",
        "#!/usr/bin/env bash\n"
        '[ -f "$LEGACY_STOP_MARKER" ] || exit 98\n'
        "printf '%s\\n' \"$@\" > \"$GATEWAY_ARGV_FILE\"\n"
        "printf '%s\\n' \"${CHATCOPILOT_INSTANCE_ID:-}\" \"${CHATCOPILOT_BOT_SPEC:-}\" "
        "\"${CHATCOPILOT_WORKSPACE_ROOT:-}\" > \"$GATEWAY_ENV_FILE\"\n",
    )

    completed = subprocess.run(
        ["bash", str(deploy / "start.sh"), "--apply-config"],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "CHATCOPILOT_HOME": str(runtime),
            "CHATCOPILOT_INSTANCE_ID": "test-bot",
            "CHATCOPILOT_BOT_SPEC": str(bot_spec),
            "CHATCOPILOT_WORKSPACE_ROOT": str(tmp_path / "workspace"),
            "CHATCOPILOT_LOG_DIR": str(tmp_path / "logs"),
            "GATEWAY_ARGV_FILE": str(argv_file),
            "GATEWAY_ENV_FILE": str(env_file),
            "LEGACY_STOP_MARKER": str(legacy_stop_marker),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert legacy_stop_marker.exists()
    assert argv_file.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "chatcopilot",
        "run",
        "--bot",
        str(bot_spec),
    ]
    assert env_file.read_text(encoding="utf-8").splitlines() == [
        "test-bot",
        str(bot_spec),
        str(tmp_path / "workspace"),
    ]
    assert not (tmp_path / "home" / ".local" / "share" / "agentstrata" / "node").exists()
    assert not (tmp_path / "runtime" / ".cc-connect").exists()


def test_dry_run_with_existing_node_does_not_fill_missing_archive_cache(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    node = runtime / "node" / "node-v24.20.0-linux-x64" / "bin" / "node"
    home.mkdir()
    _write_executable(node, "#!/bin/sh\nprintf '%s\\n' v24.20.0\n")
    _write_executable(
        fake_bin / "sha256sum",
        "#!/usr/bin/env bash\n"
        'case "${1:-}" in\n'
        f'  {node}) printf \'%s  %s\\n\' '
        "89af8424dd53e560b1933f87ba650d8bf57c83ca5a04600eefb31f416aabbae7 "
        '"$1" ;;\n'
        '  *) exec /usr/bin/sha256sum "$@" ;;\n'
        "esac\n",
    )
    cc_connect = (
        runtime
        / "node-tools"
        / "cc-connect-1.4.0-beta.3"
        / "node_modules"
        / ".bin"
        / "cc-connect"
    )
    native = cc_connect.parent.parent / "cc-connect" / "bin" / "cc-connect"
    _write_executable(cc_connect, "#!/bin/sh\nexit 0\n")
    _write_executable(native, "#!/bin/sh\nexit 0\n")
    (native.parent.parent / "package.json").write_text(
        '{"version":"1.4.0-beta.3"}\n',
        encoding="utf-8",
    )
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--no-system-packages",
            "--dry-run",
            "--no-verify",
            "--venv",
            str(tmp_path / "venv"),
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "HOME": str(home),
            "AGENTSTRATA_RUNTIME_ROOT": str(runtime),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 0, completed.stderr
    assert before == after
    assert not (runtime / "cache" / "artifacts").exists()
    assert "real run will compare the complete Node tree" in completed.stdout
    assert " ci --prefix " in completed.stdout
    assert "already installed" not in completed.stdout


def test_unlocked_cc_connect_override_is_rejected() -> None:
    completed = subprocess.run(
        ["bash", str(INSTALLER), "--cc-connect-pkg", "cc-connect@latest"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 2
    assert "only the locked package cc-connect@1.4.0-beta.3 is supported" in completed.stderr


def test_fake_uv_is_rejected_before_execution_in_real_and_dry_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    fake_uv = runtime / "uv" / "0.12.5" / "bin" / "uv"
    execution_marker = tmp_path / "fake-uv-executed"
    home.mkdir()
    fake_uv.parent.mkdir(parents=True)
    fake_uv.write_text(
        f"#!/bin/sh\n: > {execution_marker}\necho 'uv 0.12.5'\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    for mode_args in ([], ["--dry-run"]):
        completed = subprocess.run(
            [
                "bash",
                str(INSTALLER),
                "--no-system-packages",
                "--skip-cc-connect",
                "--no-verify",
                "--venv",
                str(tmp_path / "venv"),
                *mode_args,
            ],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "HOME": str(home),
                "AGENTSTRATA_RUNTIME_ROOT": str(runtime),
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        assert completed.returncode != 0
        assert "uv failed the locked binary integrity check" in completed.stderr
        assert not execution_marker.exists()


def test_same_version_fake_node_is_rejected_before_npm(tmp_path: Path) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    fake_node = runtime / "node" / "node-v24.20.0-linux-x64" / "bin" / "node"
    home.mkdir()
    fake_node.parent.mkdir(parents=True)
    fake_node.write_text("#!/bin/sh\necho 'v24.20.0'\n", encoding="utf-8")
    fake_node.chmod(0o755)

    completed = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--no-system-packages",
            "--no-verify",
            "--venv",
            str(tmp_path / "venv"),
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "HOME": str(home),
            "AGENTSTRATA_RUNTIME_ROOT": str(runtime),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode != 0
    assert "private Node failed the locked integrity check" in completed.stderr


def test_bootstrap_rejects_legacy_qq_before_installing_node(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    deploy = runtime / "deploy" / "wsl"
    deploy.mkdir(parents=True)
    for source in (BOOTSTRAP_SCRIPT, LOAD_ENV_SCRIPT):
        target = deploy / source.name
        target.write_bytes(source.read_bytes())
        target.chmod(0o755)

    install_marker = tmp_path / "installer-called"
    _write_executable(
        deploy / "install_wsl_env.sh",
        "#!/usr/bin/env bash\n"
        ': > "$INSTALL_MARKER"\n',
    )
    bot_spec = runtime / "bots" / "legacy-qq" / "bot.yaml"
    bot_spec.parent.mkdir(parents=True)
    bot_spec.write_text(
        "id: legacy-qq\nplatform:\n  type: qq\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()

    completed = subprocess.run(
        ["bash", str(deploy / "bootstrap_wsl.sh"), "--skip-apply"],
        cwd=runtime,
        env={
            **os.environ,
            "HOME": str(home),
            "CHATCOPILOT_HOME": str(runtime),
            "CHATCOPILOT_INSTANCE_ID": "legacy-qq",
            "CHATCOPILOT_BOT_SPEC": str(bot_spec),
            "INSTALL_MARKER": str(install_marker),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 78
    assert "拒绝安装 legacy Node 运行时" in completed.stderr
    assert not install_marker.exists()
