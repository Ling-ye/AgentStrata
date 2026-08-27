from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "wsl" / "quickstart.sh"


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path, *, pid_one: str = "systemd") -> tuple[Path, dict[str, str]]:
    root = tmp_path / "AgentStrata"
    script = root / "deploy" / "wsl" / "quickstart.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    shutil.copy2(REPO_ROOT / "deploy" / "wsl" / "install_wsl_env.sh", script.parent)
    shutil.copytree(
        REPO_ROOT / "deploy" / "wsl" / "node-tools",
        script.parent / "node-tools",
    )
    (root / "src" / "chatcopilot").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    shutil.copy2(REPO_ROOT / "uv.lock", root / "uv.lock")

    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "git",
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$FAKE_REPO_ROOT\"\n",
    )
    _write_executable(
        fake_bin / "ps",
        f"#!/usr/bin/env bash\nprintf '%s\\n' '{pid_one}'\n",
    )
    _write_executable(fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "df",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        "printf '%s\\n' '/dev/fake 20000000 1 19000000 1% /tmp'\n",
    )
    _write_executable(fake_bin / "dpkg-query", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "docker",
        "#!/usr/bin/env bash\n"
        "case \"${1:-}\" in\n"
        "  info) exit 0 ;;\n"
        "  context)\n"
        "    case \"${2:-}\" in\n"
        "      show) printf '%s\\n' default ;;\n"
        "      inspect) printf '%s\\n' \"${FAKE_DOCKER_ENDPOINT:-unix:///var/run/docker.sock}\" ;;\n"
        "      *) exit 1 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    _write_executable(fake_bin / "curl", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "findmnt",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"${FAKE_FINDMNT:-ext4 /dev/fake rw}\"\n",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "FAKE_REPO_ROOT": str(root),
        "HOME": str(tmp_path / "home"),
    }
    return script, env


def _last_json(stdout: str) -> dict[str, object]:
    return json.loads([line for line in stdout.splitlines() if line.startswith("{")][-1])


def _canonical_starter_text(bot_id: str = "my-assistant-qq") -> str:
    return (
        f"id: {bot_id}\n"
        'display_name: "Test Assistant"\n'
        "\n"
        "platform:\n"
        "  type: qq\n"
        "  adapter: qq_acp\n"
        "\n"
        "llm:\n"
        "  chat:\n"
        "    env_prefix: CHATCOPILOT_CHAT\n"
        "\n"
        "prompts:\n"
        "  schema_version: 2\n"
        "  identity: prompts/identity.md\n"
        "  response_style: prompts/response-style.md\n"
        "  refusal_style: prompts/refusal-style.md\n"
        "\n"
        "tools:\n"
        "  packs:\n"
        "  - workspace.read_write\n"
        "  - memory.chat\n"
        "  features:\n"
        "  - chat.file_uploads\n"
        "  - chat.private_workspace\n"
        "\n"
        "context:\n"
        "  memory_store:\n"
        "    provider: markdown\n"
        f"    namespace: {bot_id}\n"
        "\n"
        "agents:\n"
        "  backend: native\n"
        "  presets: []\n"
        "\n"
        "workspace:\n"
        "  root_env: CHATCOPILOT_WORKSPACE_ROOT\n"
        "\n"
        "deploy:\n"
        "  target: wsl2\n"
        f"  instance_id: {bot_id}\n"
        f"  wsl_home: ~/ChatCopilot-{bot_id}\n"
        f"  workspace_root: ~/chatcopilot-workspaces/{bot_id}\n"
        f"  log_dir: ~/chatcopilot-logs/{bot_id}\n"
        f"  env_file: ~/.chatcopilot-{bot_id}.env\n"
        f"  cc_connect_config_dir: ~/.chatcopilot-runtime/{bot_id}/.cc-connect\n"
        f"  project_name: chatcopilot-{bot_id}\n"
        "\n"
        "access:\n"
        "  owner_only_project_access: true\n"
    )


def _write_resume_bot(env: dict[str, str], text: str) -> Path:
    bot_dir = Path(env["FAKE_REPO_ROOT"]) / "bots" / "my-assistant-qq"
    (bot_dir / "prompts").mkdir(parents=True)
    (bot_dir / "bot.yaml").write_text(text, encoding="utf-8")
    for name in ("identity.md", "response-style.md", "refusal-style.md"):
        (bot_dir / "prompts" / name).write_text(f"# {name}\n", encoding="utf-8")
    return bot_dir


def test_dry_run_is_zero_write_and_never_prompts_for_secrets(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    before = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
    )

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
    )
    report = _last_json(completed.stdout)
    assert completed.returncode == 3
    assert before == after
    assert report["schema_version"] == "agentstrata-deployment-check/v1"
    assert report["overall"] == "needs_user_action"
    assert "API Key" not in completed.stdout
    assert "WebUI 链接" not in completed.stdout
    assert "uv 0.12.5" in completed.stdout
    assert "CPython 3.13.15" in completed.stdout
    assert "Node:       24.20.0" in completed.stdout


def test_dry_run_disables_user_curl_config_before_network_probe(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    home = Path(env["HOME"])
    home.mkdir()
    marker = tmp_path / "curl-config-side-effect"
    (home / ".curlrc").write_text(
        f"output = {marker}\n",
        encoding="utf-8",
    )
    fake_curl = Path(env["PATH"].split(":", 1)[0]) / "curl"
    _write_executable(
        fake_curl,
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" != --disable ]; then\n"
        "  : > \"$CURL_SIDE_EFFECT_MARKER\"\n"
        "fi\n"
        "exit 0\n",
    )
    env["CURL_SIDE_EFFECT_MARKER"] = str(marker)

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 3
    assert not marker.exists()
    script_text = script.read_text(encoding="utf-8")
    assert "curl --disable --fail --silent --show-error --location" in script_text
    assert "--proto '=https' --tlsv1.2" in script_text


def test_inherited_napcat_image_override_is_rejected_before_any_write(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    env["NAPCAT_IMAGE"] = "example.invalid/napcat@sha256:" + "b" * 64
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "starter_profile"
    assert "NAPCAT_IMAGE" in report["checks"][-1]["message"]


def test_inherited_runtime_root_override_is_rejected_before_any_write(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    env["AGENTSTRATA_RUNTIME_ROOT"] = str(tmp_path / "unexpected-runtime")
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    assert not (tmp_path / "unexpected-runtime").exists()
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "starter_profile"
    assert "AGENTSTRATA_RUNTIME_ROOT" in report["checks"][-1]["message"]


def test_inherited_python_bin_is_never_executed(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    marker = tmp_path / "inherited-python-executed"
    inherited_python = tmp_path / "inherited-python"
    _write_executable(
        inherited_python,
        f"#!/usr/bin/env bash\n: > \"{marker}\"\nprintf '{{}}\\n'\n",
    )
    env["PYTHON_BIN"] = str(inherited_python)

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 3
    assert not marker.exists()
    report = _last_json(completed.stdout)
    assert report["overall"] == "needs_user_action"


def test_inherited_deploy_python_override_is_rejected_before_any_write(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    env["AGENTSTRATA_DEPLOY_PYTHON"] = str(tmp_path / "untrusted-python")

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 1
    assert not Path(env["HOME"]).exists()
    report = _last_json(completed.stdout)
    assert "AGENTSTRATA_DEPLOY_PYTHON" in report["checks"][-1]["message"]


def test_missing_systemd_is_needs_user_action_before_any_write(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path, pid_one="not-systemd")

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 3
    report = _last_json(completed.stdout)
    assert report["overall"] == "needs_user_action"
    assert any(item["id"] == "systemd_pid1" for item in report["checks"])
    assert "systemd=true" in completed.stderr
    assert "wsl --shutdown" in completed.stderr
    assert not (Path(env["HOME"])).exists()


def test_missing_dbus_package_is_previewed_before_user_bus_recheck(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    _write_executable(
        tmp_path / "bin" / "systemctl",
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = --user ] && [ "${2:-}" = show-environment ]; then exit 1; fi\n'
        "exit 0\n",
    )

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 3
    report = _last_json(completed.stdout)
    user_bus = next(
        item for item in report["checks"] if item["id"] == "systemd_user_bus"
    )
    assert user_bus["status"] == "not_tested"
    assert "dbus-user-session" in user_bus["message"]
    assert "dbus-user-session" in completed.stdout
    assert not Path(env["HOME"]).exists()


def test_systemd_user_bus_recheck_stays_after_base_package_install() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    main = text.index("\nread_os_release\n")
    collect = text.index("collect_packages\n", main)
    preflight = text.index("check_systemd_user_bus preflight\n", collect)
    dry_run = text.index('if [ "$DRY_RUN" -eq 1 ]; then', preflight)
    install = text.index("ensure_base_packages \\\n", dry_run)
    recheck = text.index("check_systemd_user_bus after-install\n", install)

    assert collect < preflight < dry_run < install < recheck
    assert "dbus-user-session" in text[
        text.index("collect_packages() {") : text.index("package_is_missing() {")
    ]
    assert 'if [ "$SYSTEMD_USER_BUS_READY" -ne 1 ]; then' in text[
        install:recheck
    ]


def test_network_preflight_precedes_plan_and_any_confirmed_write() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    main = text.index("\nread_os_release\n")
    collect = text.index("collect_packages\n", main)
    preflight = text.index("check_network preflight\n", collect)
    docker = text.index("check_docker_endpoint\n", preflight)
    plan = text.index("show_plan\n", docker)
    confirm = text.index('confirm "继续执行上述 apt', plan)
    install = text.index("ensure_base_packages \\\n", confirm)
    recheck = text.index("check_network after-install\n", install)

    assert collect < preflight < docker < plan < confirm < install < recheck
    assert 'package_is_missing curl' in text[
        text.index("check_network() {") : text.index("run_bot_cli() {")
    ]


def test_systemd_pause_can_resume_before_scaffold_without_any_write(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path, pid_one="not-systemd")

    paused = subprocess.run(
        ["bash", str(script)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert paused.returncode == 3
    paused_report = _last_json(paused.stdout)
    systemd_check = next(
        item for item in paused_report["checks"] if item["id"] == "systemd_pid1"
    )
    assert "--resume" in systemd_check["remediation"]
    assert not (Path(env["FAKE_REPO_ROOT"]) / "bots" / "my-assistant-qq").exists()

    _write_executable(tmp_path / "bin" / "ps", "#!/usr/bin/env bash\nprintf '%s\\n' systemd\n")
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    resumed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )
    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    assert resumed.returncode == 3
    assert before == after
    resumed_report = _last_json(resumed.stdout)
    assert resumed_report["overall"] == "needs_user_action"
    assert any(
        item["id"] == "starter_scaffold" and item["status"] == "not_tested"
        for item in resumed_report["checks"]
    )
    assert not (Path(env["FAKE_REPO_ROOT"]) / "bots" / "my-assistant-qq").exists()


def test_systemd_pause_receipt_replays_custom_id_and_display_name(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path, pid_one="not-systemd")
    bot_id = "custom-qq"
    display_name = "Custom Assistant"
    expected = (
        "bash deploy/wsl/quickstart.sh --bot-id custom-qq "
        "--display-name Custom\\ Assistant --resume"
    )

    completed = subprocess.run(
        [
            "bash",
            str(script),
            "--bot-id",
            bot_id,
            "--display-name",
            display_name,
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 3
    report = _last_json(completed.stdout)
    systemd_check = next(
        item for item in report["checks"] if item["id"] == "systemd_pid1"
    )
    assert report["bot_id"] == bot_id
    assert expected in systemd_check["remediation"]
    assert expected in completed.stderr
    assert shlex.split(expected) == [
        "bash",
        "deploy/wsl/quickstart.sh",
        "--bot-id",
        bot_id,
        "--display-name",
        display_name,
        "--resume",
    ]
    assert not (Path(env["FAKE_REPO_ROOT"]) / "bots" / bot_id).exists()


def test_resume_rejects_existing_partial_bot_directory(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    bot_dir = Path(env["FAKE_REPO_ROOT"]) / "bots" / "my-assistant-qq"
    bot_dir.mkdir(parents=True)
    sentinel = bot_dir / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 1
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    report = _last_json(completed.stdout)
    assert report["overall"] == "failed"
    assert [item["id"] for item in report["checks"]] == ["starter_profile"]


def test_noninteractive_real_run_stops_before_mutation(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)

    completed = subprocess.run(
        ["bash", str(script)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 3
    report = _last_json(completed.stdout)
    assert any(item["id"] == "interactive_tty" for item in report["checks"])
    assert not (Path(env["HOME"])).exists()


def test_remote_docker_endpoint_is_rejected_before_any_mutation(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    env["DOCKER_HOST"] = "tcp://docker.example.invalid:2375"
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    assert report["overall"] == "failed"
    assert [item["id"] for item in report["checks"]][-1] == "docker_context"


def test_remote_docker_endpoint_is_rejected_even_without_docker_cli(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    (tmp_path / "bin" / "docker").unlink()
    env["DOCKER_HOST"] = "tcp://docker.example.invalid:2375"
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    assert report["overall"] == "failed"
    assert report["checks"][-1]["id"] == "docker_context"


def test_explicit_docker_context_is_rejected_before_mutation_without_cli(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    (tmp_path / "bin" / "docker").unlink()
    env.pop("DOCKER_HOST", None)
    env["DOCKER_CONTEXT"] = "remote-context"
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "docker_context"


def test_docker_context_precedence_cannot_mask_remote_with_local_host(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    env["DOCKER_CONTEXT"] = "remote-context"
    env["FAKE_DOCKER_ENDPOINT"] = "tcp://docker.example.invalid:2376"
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "docker_context"


def test_existing_docker_disk_config_is_rejected_without_cli_before_mutation(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    (tmp_path / "bin" / "docker").unlink()
    env.pop("DOCKER_HOST", None)
    env.pop("DOCKER_CONTEXT", None)
    docker_config = Path(env["HOME"]) / ".docker" / "config.json"
    docker_config.parent.mkdir(parents=True)
    docker_config.write_text(
        '{"currentContext":"remote-context"}\n', encoding="utf-8"
    )
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "docker_context"


def test_existing_target_refuses_normal_run_without_overwrite(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    target = Path(env["FAKE_REPO_ROOT"]) / "bots" / "my-assistant-qq"
    target.mkdir(parents=True)
    sentinel = target / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 1
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    report = _last_json(completed.stdout)
    assert report["overall"] == "failed"
    assert any(
        item["id"] == "starter_scaffold" and "拒绝覆盖" in item["message"]
        for item in report["checks"]
    )


def test_resume_canonical_starter_passes_dependency_free_preflight(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    _write_resume_bot(env, _canonical_starter_text())

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 3
    report = _last_json(completed.stdout)
    assert report["overall"] == "needs_user_action"
    assert not any(item["id"] == "starter_profile" for item in report["checks"])


def test_custom_wsl_drvfs_mount_is_rejected_before_any_write(tmp_path: Path) -> None:
    script, env = _fixture(tmp_path)
    env["FAKE_FINDMNT"] = r"9p D:\ rw,aname=drvfs;path=D:\,uid=1000"
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    check = report["checks"][-1]
    assert check["id"] == "repository_filesystem"
    assert "DrvFS/9P" in check["message"]


@pytest.mark.parametrize("unsafe_kind", ("directory", "hardlink", "wrong_mode"))
def test_resume_rejects_unsafe_local_env_before_system_mutation(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    script, env = _fixture(tmp_path)
    bot_dir = _write_resume_bot(env, _canonical_starter_text())
    local_env = bot_dir / "local.env"
    if unsafe_kind == "directory":
        local_env.mkdir()
    elif unsafe_kind == "hardlink":
        source = tmp_path / "private-source.env"
        source.write_text("SECRET=kept\n", encoding="utf-8")
        source.chmod(0o600)
        os.link(source, local_env)
    else:
        local_env.write_text("SECRET=kept\n", encoding="utf-8")
        local_env.chmod(0o640)
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--resume"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    assert [item["id"] for item in report["checks"]] == ["private_config"]


def test_resume_rejects_advanced_napcat_image_override_before_mutation(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    bot_dir = _write_resume_bot(env, _canonical_starter_text())
    local_env = bot_dir / "local.env"
    local_env.write_text(
        "export NAPCAT_IMAGE=example.invalid/napcat@sha256:"
        + "a" * 64
        + "\n",
        encoding="utf-8",
    )
    local_env.chmod(0o600)
    before = local_env.read_bytes()

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 1
    assert local_env.read_bytes() == before
    assert not Path(env["HOME"]).exists()
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "starter_profile"
    assert "NAPCAT_IMAGE" in report["checks"][-1]["message"]


def test_resume_rejects_cc_connect_executable_override_before_mutation(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    bot_dir = _write_resume_bot(env, _canonical_starter_text())
    local_env = bot_dir / "local.env"
    local_env.write_text(
        "export CHATCOPILOT_CC_CONNECT_BIN=/tmp/custom-cc-connect.js\n",
        encoding="utf-8",
    )
    local_env.chmod(0o600)
    before = local_env.read_bytes()

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 1
    assert local_env.read_bytes() == before
    assert not Path(env["HOME"]).exists()
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "starter_profile"
    assert "cc-connect" in report["checks"][-1]["message"]


def test_resume_rejects_unmanaged_runtime_module_override_before_mutation(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    bot_dir = _write_resume_bot(env, _canonical_starter_text())
    local_env = bot_dir / "local.env"
    local_env.write_text(
        "export CHATCOPILOT_HTTP_ROUTE_MODULES=unreviewed.module\n",
        encoding="utf-8",
    )
    local_env.chmod(0o600)

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 1
    assert not Path(env["HOME"]).exists()
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "starter_profile"
    assert "CHATCOPILOT_HTTP_ROUTE_MODULES" in report["checks"][-1]["message"]


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("CHATCOPILOT_CHAT_BASE_URL", "http://remote.example.invalid/v1"),
        ("QQ_ACCOUNT", "not-a-numeric-id"),
        ("QQ_ALLOW_FROM", "10001,not-a-numeric-id"),
        ("QQ_AT_PROXY_URL", "wss://remote.example.invalid:3002/ws"),
    ),
    ids=("remote-http-llm", "qq-account", "qq-allowlist", "remote-relay"),
)
def test_resume_rejects_invalid_env_value_before_system_mutation(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    script, env = _fixture(tmp_path)
    bot_dir = _write_resume_bot(env, _canonical_starter_text())
    local_env = bot_dir / "local.env"
    local_env.write_text(
        f"export {key}={shlex.quote(value)}\n",
        encoding="utf-8",
    )
    local_env.chmod(0o600)
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    assert not Path(env["HOME"]).exists()
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "private_config"
    assert key in report["checks"][-1]["message"]
    assert value not in completed.stdout
    assert value not in completed.stderr


def test_resume_dry_run_disables_bytecode_writes_in_existing_runtime_preflight(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    _write_resume_bot(env, _canonical_starter_text())
    marker = tmp_path / "preflight-bytecode-write"
    fake_python = Path(env["FAKE_REPO_ROOT"]) / ".venv" / "bin" / "python"
    _write_executable(
        fake_python,
        "#!/usr/bin/env bash\n"
        "if [ \"$#\" -eq 2 ]; then\n"
        "  [ \"${PYTHONDONTWRITEBYTECODE:-}\" = 1 ] || : > \"$BYTECODE_MARKER\"\n"
        "  exit 0\n"
        "fi\n"
        "exec \"$REAL_PYTHON\" \"$@\"\n",
    )
    env["BYTECODE_MARKER"] = str(marker)
    env["REAL_PYTHON"] = shutil.which("python3") or "python3"

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 3
    assert not marker.exists()


@pytest.mark.parametrize("unsafe_kind", ("missing", "symlink", "hardlink"))
def test_resume_rejects_unsafe_starter_prompt_before_system_mutation(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    script, env = _fixture(tmp_path)
    bot_dir = _write_resume_bot(env, _canonical_starter_text())
    prompt = bot_dir / "prompts" / "identity.md"
    if unsafe_kind == "missing":
        prompt.unlink()
    elif unsafe_kind == "symlink":
        prompt.unlink()
        prompt.symlink_to(tmp_path / "outside.md")
    else:
        source = tmp_path / "outside.md"
        source.write_text("# outside\n", encoding="utf-8")
        prompt.unlink()
        os.link(source, prompt)
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "starter_profile"


def test_resume_rejects_symlinked_prompt_parent_before_system_mutation(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    bot_dir = _write_resume_bot(env, _canonical_starter_text())
    outside = tmp_path / "outside-prompts"
    shutil.copytree(bot_dir / "prompts", outside)
    shutil.rmtree(bot_dir / "prompts")
    (bot_dir / "prompts").symlink_to(outside, target_is_directory=True)
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "starter_profile"


def test_resume_rejects_invalid_utf8_prompt_before_system_mutation(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    bot_dir = _write_resume_bot(env, _canonical_starter_text())
    prompt = bot_dir / "prompts" / "identity.md"
    prompt.write_bytes(b"# identity\n\xff\xfe\n")

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 1
    assert prompt.read_bytes() == b"# identity\n\xff\xfe\n"
    assert not Path(env["HOME"]).exists()
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "starter_profile"
    assert "UTF-8" in report["checks"][-1]["message"]


def test_resume_rejects_invalid_utf8_botspec_before_system_mutation(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    bot_dir = _write_resume_bot(env, _canonical_starter_text())
    bot_yaml = bot_dir / "bot.yaml"
    bot_yaml.write_bytes(bot_yaml.read_bytes().replace(b"Test Assistant", b"Test\xffAssistant"))

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 1
    assert b"Test\xffAssistant" in bot_yaml.read_bytes()
    assert not Path(env["HOME"]).exists()
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "starter_profile"
    assert "UTF-8" in report["checks"][-1]["message"]


def test_resume_rejects_unreadable_prompt_before_system_mutation(
    tmp_path: Path,
) -> None:
    script, env = _fixture(tmp_path)
    bot_dir = _write_resume_bot(env, _canonical_starter_text())
    prompt = bot_dir / "prompts" / "identity.md"
    prompt.chmod(0)
    try:
        completed = subprocess.run(
            ["bash", str(script), "--resume", "--dry-run"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        prompt.chmod(0o644)

    assert completed.returncode == 1
    assert prompt.read_text(encoding="utf-8") == "# identity.md\n"
    assert not Path(env["HOME"]).exists()
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "starter_profile"


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("  adapter: qq_acp\n", "  adapter: qq_acp\n  legacy: true\n"),
        ("access:\n", "advanced: true\n\naccess:\n"),
        ("  features:\n", "  mcp: {}\n  features:\n"),
        ("  adapter: qq_acp\n", "  adapter: qq_acp\n  legacy: &legacy qq\n"),
        ("  adapter: qq_acp\n", "  adapter: qq_acp\n  legacy: !str qq\n"),
        ("  adapter: qq_acp\n", "  adapter: qq_acp\n  <<: *legacy\n"),
        ("  adapter: qq_acp\n", "  adapter: qq_acp\n  adapter: qq_acp\n"),
        ("  adapter: qq_acp\n", "  adapter: qq_acp\n\tlegacy: true\n"),
    ),
    ids=(
        "unknown-platform-field",
        "unknown-top-level-field",
        "unknown-tools-flow-field",
        "yaml-anchor",
        "yaml-tag",
        "yaml-merge-alias",
        "duplicate-key",
        "tab-indentation",
    ),
)
def test_resume_rejects_noncanonical_text_before_any_mutation(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    script, env = _fixture(tmp_path)
    text = _canonical_starter_text().replace(old, new, 1)
    _write_resume_bot(env, text)
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    assert report["overall"] == "failed"
    assert [item["id"] for item in report["checks"]] == ["starter_profile"]


@pytest.mark.parametrize(
    ("old", "new"),
    (
        (
            "  adapter: qq_acp\n",
            "  adapter: qq_acp\n  adapter: qq_acp\n",
        ),
        (
            "  adapter: qq_acp\n",
            "  adapter: &adapter qq_acp\n",
        ),
        (
            "  adapter: qq_acp\n",
            "  adapter: qq_acp\n  <<: {adapter: qq_acp}\n",
        ),
    ),
    ids=("duplicate-key", "anchor", "merge-key"),
)
def test_existing_venv_resume_still_rejects_noncanonical_yaml_before_mutation(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    script, env = _fixture(tmp_path)
    _write_resume_bot(env, _canonical_starter_text().replace(old, new, 1))
    _write_executable(
        Path(env["FAKE_REPO_ROOT"]) / ".venv" / "bin" / "python",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )

    completed = subprocess.run(
        ["bash", str(script), "--resume", "--dry-run"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )

    after = sorted(
        (path.relative_to(tmp_path).as_posix(), path.read_bytes() if path.is_file() else b"")
        for path in tmp_path.rglob("*")
        if not path.is_symlink()
    )
    assert completed.returncode == 1
    assert before == after
    report = _last_json(completed.stdout)
    assert report["checks"][-1]["id"] == "starter_profile"


def test_supported_matrix_docker_safety_and_qq_order_are_locked() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for distro in (
        "ubuntu:22.04",
        "ubuntu:24.04",
        "ubuntu:26.04",
        "debian:11",
        "debian:12",
        "debian:13",
    ):
        assert distro in text
    assert "x86_64|amd64" in text
    assert "aarch64|arm64" in text
    assert "apt-get remove -y" in text
    assert text.index("apt-get install --download-only -y") < text.index("apt-get remove -y")
    assert "apt-get purge" not in text
    assert "/var/lib/docker" in text
    assert "usermod -aG docker" in text
    assert "chmod 666" not in text
    assert "curl |" not in text
    assert 'bash "$SCRIPT_DIR/qq_gateway.sh" bootstrap' in text
    assert 'webui_command login-status --wait-seconds 120' in text
    assert 'bash "$SCRIPT_DIR/qq_gateway.sh" sync-token' in text
    assert 'bash "$SCRIPT_DIR/qq_gateway.sh" status' in text
    assert text.count('if ! bash "$SCRIPT_DIR/update_instance.sh" --instance "$BOT_ID" --enable') == 1
    start = text.index('if ! bash "$SCRIPT_DIR/qq_gateway.sh" bootstrap')
    login = text.index("await_qq_login\n", start)
    sync = text.index('if ! bash "$SCRIPT_DIR/qq_gateway.sh" sync-token', login)
    status = text.index('if ! bash "$SCRIPT_DIR/qq_gateway.sh" status', sync)
    update = text.index('if ! bash "$SCRIPT_DIR/update_instance.sh"', status)
    assert start < login < sync < status < update


def test_final_ready_requires_authenticated_relay_boundary_probe() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    probe_definition = text[
        text.index("probe_relay_boundary() {") : text.index("read_webui_url() {")
    ]
    assert "require_access_token" in probe_definition
    assert "require_loopback_websocket_url" in probe_definition
    assert "probe_onebot_boundary" in probe_definition
    assert "asyncio.run(probe_onebot_boundary(url, token))" in probe_definition

    update = text.index('if ! bash "$SCRIPT_DIR/update_instance.sh"')
    unit_active = text.index("systemctl --user is-active", update)
    relay_probe = text.index("if ! probe_relay_boundary; then", unit_active)
    relay_pass = text.index('add_check "qq_relay" "pass"', relay_probe)
    ready = text.index('emit_report "ready"', relay_pass)
    assert update < unit_active < relay_probe < relay_pass < ready

    final_health_check = text[unit_active:relay_pass]
    assert "kill -0" not in final_health_check
    assert "pidfile" not in final_health_check.lower()


def test_final_ready_requires_stable_instance_bound_cc_connect_mainpid() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    definition = text[
        text.index("probe_cc_connect_main_process() {") : text.index(
            "read_webui_url() {"
        )
    ]
    assert "systemctl --user show" in definition
    assert "--property=MainPID" in definition
    assert 'grep -Fxq "$expected_node"' in definition
    assert 'grep -Fxq "$expected_entry"' in definition
    assert 'grep -Fxq "HOME=$expected_home"' in definition
    assert 'grep -Fxq "CHATCOPILOT_INSTANCE_ID=$BOT_ID"' in definition
    assert 'sleep 2' in definition
    assert '[ "$second_pid" = "$first_pid" ]' in definition

    update = text.index('if ! bash "$SCRIPT_DIR/update_instance.sh"')
    process_probe = text.index(
        'if ! probe_cc_connect_main_process "$MAIN_UNIT"', update
    )
    relay_probe = text.index("if ! probe_relay_boundary; then", process_probe)
    ready = text.index('emit_report "ready"', relay_probe)
    assert update < process_probe < relay_probe < ready
