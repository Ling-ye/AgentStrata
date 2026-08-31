from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_WRAPPER = REPO_ROOT / "deploy" / "wsl" / "bot_wrapper.sh"
DUMP_SCRIPT = REPO_ROOT / "deploy" / "wsl" / "dump.sh"
LOAD_ENV_SCRIPT = REPO_ROOT / "deploy" / "wsl" / "_load_env.sh"
BOOTSTRAP_SCRIPT = REPO_ROOT / "deploy" / "wsl" / "bootstrap_wsl.sh"
SHARED_ENV_EXAMPLE = REPO_ROOT / "deploy" / "wsl" / "env.example"


def test_shared_prefix_parser_supports_slot_legacy_and_default(
    tmp_path: Path,
) -> None:
    cases = {
        "slot.yaml": ("llm:\n  chat:\n    env_prefix: SLOT_PREFIX\n", "SLOT_PREFIX"),
        "legacy.yaml": ("llm:\n  env_prefix: LEGACY_PREFIX\n", "LEGACY_PREFIX"),
        "default.yaml": ("llm:\n  code:\n    enabled: true\n", "CHATCOPILOT_CHAT"),
    }

    for name, (content, expected) in cases.items():
        bot_spec = tmp_path / name
        bot_spec.write_text(content, encoding="utf-8")
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{LOAD_ENV_SCRIPT}"; ccp_bot_chat_env_prefix "$1"',
                "test-prefix-parser",
                str(bot_spec),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert result.stdout.strip() == expected


def test_bot_wrapper_uses_current_botspec_chat_env_prefix(tmp_path: Path) -> None:
    cases = (
        (
            "lingye",
            "CHATCOPILOT_LINGYE",
            "personal-paid-key",
            "deepseek-chat",
            (
                'export CHATCOPILOT_CHAT_API_KEY="wrong-shared-key"\n'
                'export CHATCOPILOT_CHAT_MODEL="wrong-shared-model"\n'
            ),
        ),
        (
            "sample",
            "CHATCOPILOT_CHAT",
            "sample-key",
            "dashscope/deepseek-v4-pro",
            "",
        ),
    )

    for instance, prefix, api_key, model, extra_env in cases:
        runtime = tmp_path / instance
        bot_spec = runtime / "bots" / instance / "bot.yaml"
        bot_spec.parent.mkdir(parents=True)
        bot_spec.write_text(
            "llm:\n"
            "  chat:\n"
            f"    env_prefix: {prefix}\n",
            encoding="utf-8",
        )
        fake_python = runtime / "fake-python"
        fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        fake_python.chmod(0o755)
        env_file = runtime / "runtime.env"
        env_file.write_text(
            f'export {prefix}_API_KEY="{api_key}"\n'
            f'export {prefix}_BASE_URL="https://llm.example.com"\n'
            f'export {prefix}_MODEL="{model}"\n'
            f"{extra_env}",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CHATCOPILOT_")
        }
        env.update(
            {
                "CHATCOPILOT_INSTANCE_ID": instance,
                "CHATCOPILOT_HOME": str(runtime),
                "CHATCOPILOT_BOT_SPEC": str(bot_spec),
                "CHATCOPILOT_ACP_PY": str(fake_python),
                "CHATCOPILOT_ENV_FILE": str(env_file),
            }
        )

        result = subprocess.run(
            ["bash", str(BOT_WRAPPER)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        assert f"env_prefix={prefix}" in result.stderr
        assert "API_KEY=present" in result.stderr
        assert f"MODEL={model}" in result.stderr
        assert "wrong-shared-model" not in result.stderr
        assert api_key not in result.stderr


def test_shared_diagnostics_have_no_shared_llm_or_gateway_default() -> None:
    wrapper = BOT_WRAPPER.read_text(encoding="utf-8")
    dump = DUMP_SCRIPT.read_text(encoding="utf-8")
    load_env = LOAD_ENV_SCRIPT.read_text(encoding="utf-8")

    for script in (wrapper, dump):
        assert "ccp_bot_chat_env_prefix" in script
        assert "litellm.example.invalid" not in script
        for suffix in ("API_KEY", "BASE_URL", "MODEL"):
            assert f"CHATCOPILOT_CHAT_{suffix}" not in script

    assert "ccp_bot_chat_env_prefix" in load_env
    assert "llm.chat.env_prefix" in load_env
    assert "${!CHAT_LLM_API_KEY_VAR:-}" in wrapper
    assert "${!CHAT_LLM_BASE_URL_VAR:-}" in wrapper
    assert "${!CHAT_LLM_MODEL_VAR:-}" in wrapper
    assert '${!api_key_var:-}' in dump
    assert '${!base_url_var:-}' in dump
    assert '${!model_var:-}' in dump
    assert "probe: skipped (BASE_URL not configured)" in dump


def test_missing_env_guidance_uses_the_current_bot_template() -> None:
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    shared_example = SHARED_ENV_EXAMPLE.read_text(encoding="utf-8")

    assert 'bots/${_bot_id}/local.env.example' in bootstrap
    assert 'CHATCOPILOT_INSTANCE_ID:-}" = "lingye-copilot-qq"' not in bootstrap
    assert "cp deploy/wsl/env.example ~/.chatcopilot-lingye-copilot-qq.env" not in (
        shared_example
    )
    assert (
        "cp bots/lingye-copilot-qq/local.env.example "
        "bots/lingye-copilot-qq/local.env"
    ) in shared_example


def test_bootstrap_reconciles_runtime_before_env_loader_executes_python(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    deploy = runtime / "deploy" / "wsl"
    deploy.mkdir(parents=True)
    for source in (BOOTSTRAP_SCRIPT, LOAD_ENV_SCRIPT):
        target = deploy / source.name
        target.write_bytes(source.read_bytes())
        target.chmod(0o755)

    bot_spec = runtime / "bots" / "test-bot" / "bot.yaml"
    bot_spec.parent.mkdir(parents=True)
    bot_spec.write_text("id: test-bot\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    runtime_env = home / ".chatcopilot-test-bot.env"
    runtime_env.write_text("CHATCOPILOT_CHAT_API_KEY=test\n", encoding="utf-8")
    runtime_env.chmod(0o600)

    order_log = tmp_path / "order.log"
    untrusted_marker = tmp_path / "untrusted-python-executed"
    untrusted_python = runtime / ".venv" / "bin" / "python"
    untrusted_python.parent.mkdir(parents=True)
    untrusted_python.write_text(
        "#!/usr/bin/env bash\n"
        'touch "$UNTRUSTED_PYTHON_MARKER"\n'
        'echo untrusted-python >> "$ORDER_LOG"\n'
        "exit 97\n",
        encoding="utf-8",
    )
    untrusted_python.chmod(0o755)

    safe_python = tmp_path / "reconciled-python"
    safe_python.write_text(
        "#!/usr/bin/env bash\n"
        'echo reconciled-python >> "$ORDER_LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    safe_python.chmod(0o755)
    installer = deploy / "install_wsl_env.sh"
    installer.write_text(
        "#!/usr/bin/env bash\n"
        'echo installer >> "$ORDER_LOG"\n'
        'cp "$SAFE_PYTHON" "$CHATCOPILOT_HOME/.venv/bin/python"\n'
        'chmod 755 "$CHATCOPILOT_HOME/.venv/bin/python"\n',
        encoding="utf-8",
    )
    installer.chmod(0o755)

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CHATCOPILOT_")
        and key != "AGENTSTRATA_DEPLOY_PYTHON"
    }
    env.update(
        {
            "HOME": str(home),
            "CHATCOPILOT_HOME": str(runtime),
            "CHATCOPILOT_INSTANCE_ID": "test-bot",
            "CHATCOPILOT_BOT_SPEC": str(bot_spec),
            "CHATCOPILOT_ENV_FILE": str(runtime_env),
            "ORDER_LOG": str(order_log),
            "SAFE_PYTHON": str(safe_python),
            "UNTRUSTED_PYTHON_MARKER": str(untrusted_marker),
        }
    )

    completed = subprocess.run(
        ["bash", str(deploy / "bootstrap_wsl.sh"), "--skip-apply"],
        cwd=runtime,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not untrusted_marker.exists()
    order = order_log.read_text(encoding="utf-8").splitlines()
    assert order[0] == "installer"
    assert order[1:] == [
        "reconciled-python",
        "reconciled-python",
        "reconciled-python",
    ]


def test_runtime_env_loader_never_executes_runtime_or_bashrc_text(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "executed"
    (home / ".bashrc").write_text(
        f'export CHATCOPILOT_TEST_VALUE=$(touch "{marker}")\n',
        encoding="utf-8",
    )
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        f"export CHATCOPILOT_TEST_VALUE='$(touch {marker})'\n",
        encoding="utf-8",
    )
    runtime_env.chmod(0o600)

    completed = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{LOAD_ENV_SCRIPT}"; '
            'ccp_load_env "CHATCOPILOT_TEST_VALUE"; '
            'printf "%s" "$CHATCOPILOT_TEST_VALUE"',
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "HOME": str(home),
            "CHATCOPILOT_HOME": str(REPO_ROOT),
            "CHATCOPILOT_ENV_FILE": str(runtime_env),
            "AGENTSTRATA_DEPLOY_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    assert completed.stdout == f"$(touch {marker})"
    assert not marker.exists()


def test_qq_gateway_does_not_source_bot_owned_local_env() -> None:
    script = (REPO_ROOT / "deploy/wsl/qq_gateway.sh").read_text(encoding="utf-8")

    assert 'source "$LOCAL_CONFIG"' not in script
    assert "provision_runtime_env" in script
