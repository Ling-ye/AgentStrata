from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatcopilot.botspec.cli import main as bot_cli_main
from chatcopilot.external_tools.codex_cli import auth_cli
from chatcopilot.external_tools.codex_cli.auth_cli import (
    CodexAuthOperatorConfig,
    CodexAuthOperatorError,
    login_lanes,
    validate_auth_root,
)
from chatcopilot.external_tools.codex_cli.credentials import (
    credential_lock,
    credential_status,
    install_login_credential,
)


def _write_codex(tmp_path: Path, login_body: str, *, device_auth: bool = True) -> Path:
    binary = tmp_path / "codex"
    help_line = "printf '%s\\n' --device-auth" if device_auth else "printf '%s\\n' login"
    binary.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "login" ] && [ "$2" = "--help" ]; then\n'
        f"  {help_line}\n"
        "  exit 0\n"
        "fi\n"
        f"{login_body}\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary


def _success_body(
    payload: str | None = None,
    *,
    expand_payload: bool = False,
) -> str:
    payload = payload or _auth_payload("new")
    payload_command = (
        """printf '%s\\n' "$payload" > "$CODEX_HOME/auth.json"\n"""
        if expand_payload
        else f"""printf '%s\\n' '{payload}' > "$CODEX_HOME/auth.json"\n"""
    )
    return (
        '[ "$1" = "login" ] || exit 91\n'
        '[ "$2" = "--device-auth" ] || exit 92\n'
        '[ "$3" = "-c" ] || exit 93\n'
        """[ "$4" = 'cli_auth_credentials_store="file"' ] || exit 94\n"""
        '[ "$CODEX_HOME" = "$CODEX_SQLITE_HOME" ] || exit 95\n'
        '[ "$HOME" = "$CODEX_HOME" ] || exit 96\n'
        '[ "$PWD" = "$CODEX_HOME" ] || exit 97\n'
        "umask 077\n"
        + payload_command
        + "exit 0"
    )


def _auth_payload(token: str) -> str:
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": f"id-{token}",
                "access_token": f"access-{token}",
                "refresh_token": token,
                "account_id": "test-account",
            },
            "last_refresh": "2026-07-28T00:00:00Z",
        },
        separators=(",", ":"),
    )


def _refresh_token(payload: dict[str, object]) -> str:
    tokens = payload.get("tokens")
    assert isinstance(tokens, dict)
    return str(tokens["refresh_token"])


def _config(tmp_path: Path, binary: Path) -> CodexAuthOperatorConfig:
    return CodexAuthOperatorConfig.from_values(
        str(tmp_path / "authority"),
        str(binary),
    )


def test_login_all_runs_two_device_authorizations_and_installs_both_lanes(
    tmp_path: Path,
) -> None:
    counter = tmp_path / "counter"
    binary = _write_codex(
        tmp_path,
        (
            f'if [ -f "{counter}" ]; then\n'
            f"  payload='{_auth_payload('worker')}'\n"
            "else\n"
            f'  : > "{counter}"\n'
            f"  payload='{_auth_payload('main')}'\n"
            "fi\n"
            + _success_body(expand_payload=True)
        ),
    )
    config = _config(tmp_path, binary)

    results = login_lanes(config, "all")

    assert [(result.lane, result.ok, result.generation) for result in results] == [
        ("main", True, 1),
        ("worker", True, 1),
    ]
    main_payload = json.loads((config.auth_root / "auth.json").read_text())
    worker_payload = json.loads(
        (config.auth_root / "worker" / "auth.json").read_text()
    )
    assert _refresh_token(main_payload) == "main"
    assert _refresh_token(worker_payload) == "worker"
    assert stat_mode(config.auth_root) == 0o700
    assert stat_mode(config.auth_root / "auth.json") == 0o600
    assert stat_mode(config.auth_root / "worker") == 0o700
    assert stat_mode(config.auth_root / "worker" / "auth.json") == 0o600


def test_cancelled_login_preserves_authority_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _write_codex(tmp_path, _success_body())
    config = _config(tmp_path, binary)
    first = login_lanes(config, "main")
    old = (config.auth_root / "auth.json").read_bytes()
    staging_paths: list[Path] = []

    def cancel(_config: object, staging_home: Path) -> str:
        staging_paths.append(staging_home)
        (staging_home / "auth.json").write_text('{"token":"partial"}')
        (staging_home / "auth.json").chmod(0o600)
        return "device_auth_cancelled"

    monkeypatch.setattr(auth_cli, "_run_device_login", cancel)
    result = login_lanes(config, "main")

    assert first[0].ok
    assert result[0].error_code == "device_auth_cancelled"
    assert (config.auth_root / "auth.json").read_bytes() == old
    assert staging_paths and not staging_paths[0].exists()


def test_timeout_preserves_authority_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _write_codex(tmp_path, _success_body())
    config = _config(tmp_path, binary)
    staging_paths: list[Path] = []

    def timeout(_config: object, staging_home: Path) -> str:
        staging_paths.append(staging_home)
        return "device_auth_timeout"

    monkeypatch.setattr(auth_cli, "_run_device_login", timeout)

    result = login_lanes(config, "worker")

    assert result[0].error_code == "device_auth_timeout"
    assert not (config.auth_root / "worker" / "auth.json").exists()
    assert staging_paths and not staging_paths[0].exists()


def test_device_login_hard_timeout_terminates_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _write_codex(tmp_path, "/bin/sleep 5\nexit 0")
    config = _config(tmp_path, binary)
    monkeypatch.setattr(auth_cli, "_LOGIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(auth_cli, "_TERMINATE_GRACE_SECONDS", 0.2)

    result = login_lanes(config, "main")

    assert result[0].error_code == "device_auth_timeout"
    assert not (config.auth_root / "auth.json").exists()


def test_missing_fixed_binary_is_rejected_without_path_fallback(tmp_path: Path) -> None:
    with pytest.raises(CodexAuthOperatorError) as error:
        CodexAuthOperatorConfig.from_values(
            str(tmp_path / "authority"),
            str(tmp_path / "missing-codex"),
        )

    assert error.value.code == "codex_binary_missing"


def test_device_auth_unsupported_reports_only_stable_code(tmp_path: Path) -> None:
    binary = _write_codex(tmp_path, "exit 99", device_auth=False)
    config = _config(tmp_path, binary)

    results = login_lanes(config, "all")

    assert [result.error_code for result in results] == [
        "device_auth_unsupported",
        "device_auth_unsupported",
    ]
    assert not config.auth_root.exists()


def test_device_auth_preflight_timeout_reports_safe_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _write_codex(
        tmp_path,
        (
            "exit 99"
        ),
    )
    binary.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "login" ] && [ "$2" = "--help" ]; then\n'
        "  /bin/sleep 5\n"
        "  exit 0\n"
        "fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    config = _config(tmp_path, binary)
    monkeypatch.setattr(auth_cli, "_PREFLIGHT_TIMEOUT_SECONDS", 0.05)

    results = login_lanes(config, "all")

    assert [result.error_code for result in results] == [
        "device_auth_preflight_timeout",
        "device_auth_preflight_timeout",
    ]


def test_staging_cleanup_failure_becomes_safe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _write_codex(tmp_path, _success_body())
    config = _config(tmp_path, binary)
    real_rmtree = auth_cli.shutil.rmtree
    retained: list[Path] = []

    def fail_without_removing(path: Path) -> None:
        retained.append(path)
        raise OSError("do not expose this path or text")

    monkeypatch.setattr(auth_cli.shutil, "rmtree", fail_without_removing)

    results = login_lanes(config, "main")

    assert results[0].error_code == "staging_cleanup_failed"
    assert not config.auth_root.exists()
    monkeypatch.setattr(auth_cli.shutil, "rmtree", real_rmtree)
    for path in retained:
        real_rmtree(path, ignore_errors=True)


def test_lane_staging_cleanup_failure_preserves_old_credential_and_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _write_codex(tmp_path, _success_body(_auth_payload("old")))
    config = _config(tmp_path, binary)
    assert login_lanes(config, "main")[0].ok
    old_auth = (config.auth_root / "auth.json").read_bytes()
    old_generation = credential_status(config.auth_root, "main").generation
    _write_codex(tmp_path, _success_body(_auth_payload("new")))
    real_rmtree = auth_cli.shutil.rmtree
    cleanup_calls = 0
    retained: list[Path] = []

    def fail_lane_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 2:
            retained.append(path)
            raise OSError("private cleanup detail")
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_cli.shutil, "rmtree", fail_lane_cleanup)
    try:
        results = login_lanes(config, "main")
    finally:
        monkeypatch.setattr(auth_cli.shutil, "rmtree", real_rmtree)
        for path in retained:
            real_rmtree(path, ignore_errors=True)

    assert results[0].error_code == "staging_cleanup_failed"
    assert (config.auth_root / "auth.json").read_bytes() == old_auth
    assert credential_status(config.auth_root, "main").generation == old_generation


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    (
        ("not-json", "staging_auth_invalid_json"),
        (
            json.dumps(
                {"auth_mode": "apikey", "OPENAI_API_KEY": "forbidden"},
                separators=(",", ":"),
            ),
            "staging_auth_unsupported_mode",
        ),
    ),
)
def test_invalid_device_login_output_preserves_old_authority_and_cleans_staging(
    tmp_path: Path,
    payload: str,
    expected_code: str,
) -> None:
    binary = _write_codex(tmp_path, _success_body(_auth_payload("old")))
    config = _config(tmp_path, binary)
    assert login_lanes(config, "main")[0].ok
    old_auth = (config.auth_root / "auth.json").read_bytes()
    old_generation = credential_status(config.auth_root, "main").generation
    staging_record = tmp_path / "login-staging-path"
    _write_codex(
        tmp_path,
        f"""printf '%s' "$CODEX_HOME" > '{staging_record}'\n"""
        + _success_body(payload),
    )

    results = login_lanes(config, "main")

    assert results[0].error_code == expected_code
    assert (config.auth_root / "auth.json").read_bytes() == old_auth
    assert credential_status(config.auth_root, "main").generation == old_generation
    assert staging_record.exists()
    assert not Path(staging_record.read_text(encoding="utf-8")).exists()


@pytest.mark.parametrize("old_value", ("not-json", _auth_payload("old")))
def test_successful_login_repairs_invalid_or_permissive_old_authority(
    tmp_path: Path,
    old_value: str,
) -> None:
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    old_auth = root / "auth.json"
    old_auth.write_text(old_value, encoding="utf-8")
    old_auth.chmod(0o644 if old_value != "not-json" else 0o600)
    binary = _write_codex(tmp_path, _success_body(_auth_payload("new")))
    config = _config(tmp_path, binary)

    results = login_lanes(config, "main")

    assert results[0].ok
    assert stat_mode(old_auth) == 0o600
    assert _refresh_token(json.loads(old_auth.read_text(encoding="utf-8"))) == "new"
    assert credential_status(root, "main").generation == 1


@pytest.mark.parametrize(
    "value",
    (
        str(Path("~").expanduser() / ".codex"),
        str(Path("~").expanduser() / ".codex" / "bot-auth"),
        "/mnt/c/" + "Users/Example/.codex",
        "/mnt/c/" + "Users/Example/.codex/bot-auth",
        "/mnt/d/" + "users/Example/.CODEX",
    ),
)
def test_personal_and_desktop_auth_roots_are_forbidden(value: str) -> None:
    with pytest.raises(CodexAuthOperatorError) as error:
        validate_auth_root(value)

    assert error.value.code == "auth_root_personal_forbidden"


def test_configured_personal_codex_home_is_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal = tmp_path / "personal-codex"
    monkeypatch.setenv("CODEX_HOME", str(personal))

    with pytest.raises(CodexAuthOperatorError) as error:
        validate_auth_root(str(personal))

    assert error.value.code == "auth_root_personal_forbidden"


def test_descendant_of_configured_personal_codex_home_is_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal = tmp_path / "personal-codex"
    monkeypatch.setenv("CODEX_HOME", str(personal))

    with pytest.raises(CodexAuthOperatorError) as error:
        validate_auth_root(str(personal / "bot-auth"))

    assert error.value.code == "auth_root_personal_forbidden"


def test_symlink_alias_to_configured_personal_home_is_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal = tmp_path / "personal-codex"
    personal.mkdir()
    alias = tmp_path / "authority-alias"
    alias.symlink_to(personal, target_is_directory=True)
    monkeypatch.setenv("CHATCOPILOT_CODEX_HOME", str(personal))

    with pytest.raises(CodexAuthOperatorError) as error:
        validate_auth_root(str(alias))

    assert error.value.code == "auth_root_personal_forbidden"


def test_busy_lane_fails_without_touching_existing_credential(tmp_path: Path) -> None:
    binary = _write_codex(tmp_path, _success_body())
    config = _config(tmp_path, binary)
    first = login_lanes(config, "main")
    old = (config.auth_root / "auth.json").read_bytes()

    with credential_lock(config.auth_root, "main", blocking=False):
        result = login_lanes(config, "main")

    assert first[0].ok
    assert result[0].error_code == "lock_busy"
    assert (config.auth_root / "auth.json").read_bytes() == old


def test_partial_all_login_keeps_success_and_preserves_other_lane(
    tmp_path: Path,
) -> None:
    counter = tmp_path / "counter"
    binary = _write_codex(
        tmp_path,
        (
            f'if [ -f "{counter}" ]; then exit 12; fi\n'
            f': > "{counter}"\n'
            + _success_body(_auth_payload("main"))
        ),
    )
    config = _config(tmp_path, binary)
    worker_staging = tmp_path / "worker-seed"
    worker_staging.mkdir(mode=0o700)
    (worker_staging / "auth.json").write_text(_auth_payload("old-worker"))
    (worker_staging / "auth.json").chmod(0o600)

    install_login_credential(config.auth_root, "worker", worker_staging)
    old_worker = (config.auth_root / "worker" / "auth.json").read_bytes()

    results = login_lanes(config, "all")

    assert [(result.lane, result.ok) for result in results] == [
        ("main", True),
        ("worker", False),
    ]
    assert _refresh_token(
        json.loads((config.auth_root / "auth.json").read_text())
    ) == "main"
    assert (config.auth_root / "worker" / "auth.json").read_bytes() == old_worker


def test_status_json_is_safe_and_uses_only_local_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    binary = _write_codex(tmp_path, _success_body(_auth_payload("never-print")))
    config = _config(tmp_path, binary)
    assert login_lanes(config, "main")[0].ok
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    config_path = bot_dir / "operator.env"
    config_path.write_text(
        f"CHATCOPILOT_CODEX_BOT_HOME={config.auth_root}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "chatcopilot.botspec.cli.load_botspec",
        lambda _path: SimpleNamespace(base_dir=bot_dir),
    )
    monkeypatch.setenv("CHATCOPILOT_CODEX_BOT_HOME", str(tmp_path / "wrong"))

    code = bot_cli_main(
        [
            "codex-auth",
            "status",
            "--bot",
            "ignored.yaml",
            "--config",
            str(config_path),
            "--lane",
            "all",
            "--json",
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert code == 0
    assert [item["state"] for item in payload["lanes"]] == ["ready", "missing"]
    assert "never-print" not in output
    assert str(config.auth_root) not in output
    assert str(binary) not in output
    assert set(payload) == {"lanes"}
    expected_lane_keys = {
        "lane",
        "state",
        "credential_updated_at",
        "installed_at",
        "refreshed_at",
        "error_code",
    }
    assert all(set(item) == expected_lane_keys for item in payload["lanes"])
    assert "generation" not in output


def test_status_json_config_failure_has_safe_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    monkeypatch.setattr(
        "chatcopilot.botspec.cli.load_botspec",
        lambda _path: SimpleNamespace(base_dir=bot_dir),
    )

    code = bot_cli_main(
        [
            "codex-auth",
            "status",
            "--bot",
            "ignored.yaml",
            "--lane",
            "main",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload == {"error_code": "auth_config_missing", "lanes": []}


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
