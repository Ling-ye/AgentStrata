from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_code_worker_unit_keeps_wsl_compatible_hardening() -> None:
    unit = _read("console/systemd/chatcopilot-code-worker@.service")

    assert "-m chatcopilot.code_task_service" in unit
    assert "ProtectKernelLogs=" not in unit
    assert "ProtectKernelModules=" not in unit
    for directive in (
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectControlGroups=true",
        "ProtectKernelTunables=true",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        "MemoryMax=256M",
        "TasksMax=64",
    ):
        assert directive in unit


def test_registration_accepts_exported_worker_values_and_pins_main_instance() -> None:
    script = _read("console/systemd/register.sh")

    assert 'key = re.sub(r"^export\\s+", "", key)' in script
    assert 'echo "CHATCOPILOT_INSTANCE_ID=$iid"' in script
    assert 'echo "CHATCOPILOT_BOT_SPEC=$deployed_bot"' in script
    assert 'workspace_root_raw="$(read_deploy_value "$bot" workspace_root)"' in script
    assert 'workspace_root_raw="~/chatcopilot-workspaces/$iid"' in script
    assert 'CHATCOPILOT_REGISTER_WORKSPACE_ROOT="$workspace_root"' in script
    assert 'values["CHATCOPILOT_WORKSPACE_ROOT"] = os.environ[' in script
    assert 'if [ -L "$CONSOLE_CONF_DIR" ]' in script
    assert 'chmod 700 "$CONSOLE_CONF_DIR"' in script
    assert 'getattr(os, "O_DIRECTORY", 0)' in script
    assert "require_private_parent(target)" in script
    assert "stat.S_IMODE(info.st_mode) != 0o600" in script
    assert 'getattr(os, "O_NOFOLLOW", 0)' in script
    assert "info = os.fstat(fd)" in script
    assert "info.st_nlink != 1" in script
    assert "path.chmod(0o600)" not in script
    assert "def read_dev_paths" not in script
    assert "import ast" not in script
    assert "instance_code_policy = re.compile" in script
    assert "PROFILES_JSON|TASK_PROFILE" in script
    assert '    "CHATCOPILOT_CODE_MODEL",' not in script
    assert '    "CHATCOPILOT_CODE_REASONING_EFFORT",' not in script
    assert '"CHATCOPILOT_CODEX_BIN"' in script
    assert '"CHATCOPILOT_CODEX_BOT_HOME"' in script
    assert '"CHATCOPILOT_CODE_TASK_GITHUB_ACTOR"' in script
    assert 'actor = values["CHATCOPILOT_CODE_TASK_GITHUB_ACTOR"]' in script
    assert "CHATCOPILOT_CODE_TASK_GITHUB_ACTOR is malformed" in script


def test_code_worker_bootstrap_uses_canonical_botspec_runtime_env() -> None:
    bootstrap = _read("src/chatcopilot/code_task_service.py")

    assert "load_runtime_context()" in bootstrap
    assert "apply_runtime_env(runtime)" in bootstrap
    assert "runtime.instance_id != configured_instance" in bootstrap
    assert "external_tools.dev.code_task_service import main" in bootstrap


def test_runtime_bot_selection_prefers_requested_instance_over_public_default() -> None:
    script = _read("deploy/wsl/_load_env.sh")
    instance_candidate = (
        '$CCP_HOME_DEFAULT/bots/$CHATCOPILOT_INSTANCE_ID/bot.yaml'
    )
    default_candidate = "$CCP_HOME_DEFAULT/bots/lingye-copilot-qq/bot.yaml"

    assert instance_candidate in script
    assert script.index(instance_candidate) < script.index(default_candidate)
