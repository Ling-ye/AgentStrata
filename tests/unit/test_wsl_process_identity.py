from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time


REPO_ROOT = Path(__file__).resolve().parents[2]
STATUS_SCRIPT = REPO_ROOT / "deploy" / "wsl" / "status.sh"


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    deploy_dir = tmp_path / "runtime" / "deploy" / "wsl"
    deploy_dir.mkdir(parents=True)
    for name in ("_load_env.sh", "_stop_cc.sh"):
        shutil.copy2(REPO_ROOT / "deploy" / "wsl" / name, deploy_dir / name)

    home = tmp_path / "home"
    cc_home = tmp_path / "cc-home"
    config_dir = cc_home / ".cc-connect"
    home.mkdir()
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[[projects.platforms]]\ntype = "qq"\n',
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    _write_executable(fake_bin / "pgrep", "#!/usr/bin/env bash\nexit 1\n")
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "CHATCOPILOT_HOME": str(tmp_path / "runtime"),
        "CHATCOPILOT_INSTANCE_ID": "test-bot",
        "CHATCOPILOT_CC_HOME": str(cc_home),
        "CHATCOPILOT_CC_CONNECT_CONFIG_DIR": str(config_dir),
        "CHATCOPILOT_LOG_DIR": str(tmp_path / "logs"),
    }
    return deploy_dir, deploy_dir / "_stop_cc.sh", cc_home, env


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def test_stop_ignores_stale_pidfiles_pointing_to_unrelated_process(
    tmp_path: Path,
) -> None:
    _, stop_script, cc_home, env = _fixture(tmp_path)
    unrelated = subprocess.Popen(["/bin/sleep", "60"])
    try:
        (cc_home / "qq-at-proxy.pid").write_text(
            f"{unrelated.pid}\n", encoding="utf-8"
        )
        (cc_home / "cc-connect.pid").write_text(
            f"{unrelated.pid}\n", encoding="utf-8"
        )

        completed = subprocess.run(
            ["bash", str(stop_script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert unrelated.poll() is None
        assert "忽略未绑定当前实例的残留 QQ Relay" in completed.stderr
        assert "忽略未绑定当前实例的残留 cc-connect" in completed.stderr
        assert not (cc_home / "qq-at-proxy.pid").exists()
        assert not (cc_home / "cc-connect.pid").exists()
    finally:
        _terminate(unrelated)


def test_stop_still_terminates_processes_bound_to_current_instance(
    tmp_path: Path,
) -> None:
    _, stop_script, cc_home, env = _fixture(tmp_path)
    relay = tmp_path / "relay-fixture"
    cc_connect = tmp_path / "cc-connect-fixture"
    loop = (
        "#!/usr/bin/env bash\n"
        "trap 'exit 0' TERM INT\n"
        "while :; do /bin/sleep 0.05; done\n"
    )
    _write_executable(relay, loop)
    _write_executable(cc_connect, loop)
    relay_process = subprocess.Popen(
        [str(relay), "-m", "chatcopilot", "qq-at-proxy"],
        env={
            **env,
            "CHATCOPILOT_INSTANCE_ID": "test-bot",
            "CHATCOPILOT_CC_HOME": str(cc_home),
        },
    )
    cc_process = subprocess.Popen(
        [str(cc_connect)],
        env={
            **env,
            "HOME": str(cc_home),
            "CHATCOPILOT_INSTANCE_ID": "test-bot",
        },
    )
    try:
        time.sleep(0.1)
        (cc_home / "qq-at-proxy.pid").write_text(
            f"{relay_process.pid}\n", encoding="utf-8"
        )
        (cc_home / "cc-connect.pid").write_text(
            f"{cc_process.pid}\n", encoding="utf-8"
        )

        completed = subprocess.run(
            ["bash", str(stop_script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

        assert completed.returncode == 0, completed.stdout + completed.stderr
        relay_process.wait(timeout=3)
        cc_process.wait(timeout=3)
        assert "停止 QQ @ Relay" in completed.stdout
        assert "停止 cc-connect" in completed.stdout
    finally:
        _terminate(relay_process)
        _terminate(cc_process)


def test_stop_finds_instance_relay_when_legacy_pidfile_is_missing(
    tmp_path: Path,
) -> None:
    _, stop_script, cc_home, env = _fixture(tmp_path)
    relay = tmp_path / "relay-fixture"
    _write_executable(
        relay,
        "#!/usr/bin/env bash\n"
        "trap 'exit 0' TERM INT\n"
        "while :; do /bin/sleep 0.05; done\n",
    )
    relay_process = subprocess.Popen(
        [str(relay), "-m", "chatcopilot", "qq-at-proxy"],
        env={
            **env,
            "CHATCOPILOT_INSTANCE_ID": "test-bot",
            "CHATCOPILOT_CC_HOME": str(cc_home),
        },
    )
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    _write_executable(
        fake_bin / "pgrep",
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$RELAY_PID"\n',
    )
    try:
        time.sleep(0.1)
        completed = subprocess.run(
            ["bash", str(stop_script)],
            env={**env, "RELAY_PID": str(relay_process.pid)},
            capture_output=True,
            text=True,
            timeout=15,
        )

        assert completed.returncode == 0, completed.stdout + completed.stderr
        relay_process.wait(timeout=3)
        assert "停止 QQ @ Relay" in completed.stdout
    finally:
        _terminate(relay_process)


def test_status_rejects_stale_pidfile_pointing_to_unrelated_process(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    deploy = runtime / "deploy" / "wsl"
    deploy.mkdir(parents=True)
    shutil.copy2(STATUS_SCRIPT, deploy / "status.sh")
    shutil.copy2(REPO_ROOT / "deploy" / "wsl" / "_load_env.sh", deploy / "_load_env.sh")
    bot = runtime / "bots" / "test-bot" / "bot.yaml"
    bot.parent.mkdir(parents=True)
    bot.write_text(
        "id: test-bot\n"
        "display_name: test\n"
        "platform:\n"
        "  type: qq\n"
        "  adapter: qq_acp\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    cc_home = tmp_path / "cc-home"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    cc_home.mkdir()
    _write_executable(fake_bin / "pgrep", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 1\n")
    unrelated = subprocess.Popen(["/bin/sleep", "60"])
    try:
        (cc_home / "cc-connect.pid").write_text(
            f"{unrelated.pid}\n", encoding="utf-8"
        )
        completed = subprocess.run(
            [
                "bash",
                str(deploy / "status.sh"),
                "--instance",
                "test-bot",
                "--bot-spec",
                str(bot),
            ],
            env={
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "CHATCOPILOT_HOME": str(runtime),
                "CHATCOPILOT_CC_HOME": str(cc_home),
                "CHATCOPILOT_CC_CONNECT_CONFIG_DIR": str(cc_home / ".cc-connect"),
                "CHATCOPILOT_LOG_DIR": str(tmp_path / "logs"),
            },
            capture_output=True,
            text=True,
            timeout=15,
        )

        assert completed.returncode == 0, completed.stderr
        assert unrelated.poll() is None
        assert "本实例 cc-connect 未运行" in completed.stdout
        assert "本实例 cc-connect 在运行" not in completed.stdout
    finally:
        _terminate(unrelated)
