from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_HELPER_PATH = _ROOT / "deploy" / "docker" / "desired_state.py"
_SCRIPT_PATH = _ROOT / "deploy" / "docker" / "services.sh"
_SPEC = importlib.util.spec_from_file_location("docker_desired_state", _HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
desired_state = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(desired_state)


def _write_bot(
    root: Path,
    name: str,
    *,
    providers: str = "[]",
    mcp_servers: str = "servers: []\n",
    unified_enabled: str | None = "true",
    search_block_name: str = "unified_search",
) -> Path:
    bot_dir = root / "bots" / name
    (bot_dir / "mcp").mkdir(parents=True)
    (bot_dir / "persona.md").write_text("test bot\n", encoding="utf-8")
    (bot_dir / "mcp" / "servers.yaml").write_text(mcp_servers, encoding="utf-8")
    bot_path = bot_dir / "bot.yaml"
    bot_path.write_text(
        "\n".join(
            (
                f"id: {name}",
                f"display_name: {name}",
                "platform:",
                "  type: qq",
                "  adapter: qq_acp",
                "prompts:",
                "  persona: persona.md",
                "tools:",
                "  packs: []",
                "  mcp:",
                "    servers: mcp/servers.yaml",
                "agents:",
                "  backend: native",
                f"  {search_block_name}:",
                *(
                    (f"    enabled: {unified_enabled}",)
                    if unified_enabled is not None
                    else ()
                ),
                "    providers:",
                *(f"      {line}" for line in providers.splitlines()),
                "deploy:",
                "  target: wsl2",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return bot_path


def test_discovery_rejects_zero_botspecs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHATCOPILOT_BOT_SPECS", raising=False)
    monkeypatch.delenv("CHATCOPILOT_BOT_SPEC", raising=False)

    with pytest.raises(desired_state.DesiredStateError, match="no BotSpec files"):
        desired_state.discover_bot_specs(tmp_path)


def test_resolver_requires_at_least_one_valid_botspec() -> None:
    with pytest.raises(desired_state.DesiredStateError, match="at least one valid BotSpec"):
        desired_state.resolve_desired_services(())


def test_cli_rejects_zero_discovered_botspecs(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("CHATCOPILOT_BOT_SPECS", None)
    env.pop("CHATCOPILOT_BOT_SPEC", None)

    result = subprocess.run(
        (
            sys.executable,
            str(_HELPER_PATH),
            "--repo-root",
            str(tmp_path),
        ),
        cwd=_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 2
    assert "no BotSpec files were discovered" in result.stderr


def test_resolver_aggregates_direct_provider_and_enabled_mcp_bindings(tmp_path: Path) -> None:
    first = _write_bot(
        tmp_path,
        "first-bot",
        providers="- id: local\n  kind: searxng\n  enabled: true",
        mcp_servers=(
            "servers:\n"
            "  - ref: playwright-browser\n"
            "    enabled: true\n"
            "  - ref: xiaohongshu-search\n"
            "    enabled: false\n"
        ),
    )
    second = _write_bot(
        tmp_path,
        "second-bot",
        mcp_servers="servers:\n  - ref: xiaohongshu-search\n    enabled: true\n",
    )

    assert desired_state.resolve_desired_services((second, first)) == (
        "searxng",
        "playwright-mcp",
        "xiaohongshu-mcp",
    )


def test_resolver_treats_strict_false_strings_as_disabled(tmp_path: Path) -> None:
    bot = _write_bot(
        tmp_path,
        "disabled-bot",
        providers='- id: local\n  kind: searxng\n  enabled: "false"',
        mcp_servers='servers:\n  - ref: playwright-browser\n    enabled: "off"\n',
        unified_enabled='"true"',
    )

    assert desired_state.resolve_desired_services((bot,)) == ()


def test_resolver_uses_canonical_legacy_search_alias(tmp_path: Path) -> None:
    bot = _write_bot(
        tmp_path,
        "legacy-alias",
        providers="- id: local\n  kind: searxng\n  enabled: true",
        search_block_name="research_router",
    )

    assert desired_state.resolve_desired_services((bot,)) == ("searxng",)


def test_resolver_respects_canonical_disabled_default(tmp_path: Path) -> None:
    bot = _write_bot(
        tmp_path,
        "disabled-by-default",
        providers="- id: local\n  kind: searxng\n  enabled: true",
        unified_enabled=None,
    )

    assert desired_state.resolve_desired_services((bot,)) == ()


def test_resolver_respects_canonical_mcp_disabled_exposure(tmp_path: Path) -> None:
    bot = _write_bot(
        tmp_path,
        "disabled-exposure",
        mcp_servers=(
            "servers:\n"
            "  - ref: playwright-browser\n"
            "    enabled: true\n"
            "    exposure: disabled\n"
        ),
    )

    assert desired_state.resolve_desired_services((bot,)) == ()


def test_custom_mcp_server_id_does_not_impersonate_reviewed_service(
    tmp_path: Path,
) -> None:
    bot = _write_bot(
        tmp_path,
        "custom-playwright-id",
        mcp_servers=(
            "servers:\n"
            "  - id: playwright\n"
            "    enabled: true\n"
            "    transport: streamable_http\n"
            "    url: http://127.0.0.1:19066/mcp\n"
            "    exposure: subagent\n"
            "    risk: interactive\n"
        ),
    )

    assert desired_state.resolve_desired_services((bot,)) == ()


def test_custom_mcp_cannot_forge_reviewed_catalog_provenance(tmp_path: Path) -> None:
    bot = _write_bot(
        tmp_path,
        "forged-catalog-ref",
        mcp_servers=(
            "servers:\n"
            "  - id: custom-browser\n"
            "    catalog_ref: playwright-browser\n"
            "    enabled: true\n"
            "    transport: streamable_http\n"
            "    url: http://127.0.0.1:19066/mcp\n"
            "    exposure: subagent\n"
            "    risk: interactive\n"
        ),
    )

    with pytest.raises(
        desired_state.DesiredStateError,
        match=r"BotSpec validation failed:.*catalog_ref.*runtime",
    ):
        desired_state.resolve_desired_services((bot,))


def test_resolver_rejects_ambiguous_boolean_before_reconciliation(tmp_path: Path) -> None:
    bot = _write_bot(
        tmp_path,
        "invalid-bot",
        providers='- id: local\n  kind: searxng\n  enabled: sometimes',
    )

    with pytest.raises(desired_state.DesiredStateError, match="boolean"):
        desired_state.resolve_desired_services((bot,))


def test_resolver_rejects_ambiguous_global_search_boolean(tmp_path: Path) -> None:
    bot = _write_bot(tmp_path, "invalid-global-bool", unified_enabled="sometimes")

    with pytest.raises(desired_state.DesiredStateError, match="boolean"):
        desired_state.resolve_desired_services((bot,))


def test_resolver_rejects_ambiguous_mcp_boolean(tmp_path: Path) -> None:
    bot = _write_bot(
        tmp_path,
        "invalid-mcp-bool",
        mcp_servers=(
            "servers:\n"
            "  - ref: playwright-browser\n"
            "    enabled: sometimes\n"
        ),
    )

    with pytest.raises(
        desired_state.DesiredStateError,
        match=r"BotSpec validation failed:.*mcp\.servers\[0\]\.enabled",
    ):
        desired_state.resolve_desired_services((bot,))


def test_resolver_rejects_missing_mcp_binding_file(tmp_path: Path) -> None:
    bot = _write_bot(tmp_path, "missing-mcp")
    (bot.parent / "mcp" / "servers.yaml").unlink()

    with pytest.raises(
        desired_state.DesiredStateError,
        match=r"BotSpec validation failed:.*tools\.mcp\.servers",
    ):
        desired_state.resolve_desired_services((bot,))


def test_resolver_rejects_unrelated_fatal_botspec_error(tmp_path: Path) -> None:
    bot = _write_bot(tmp_path, "missing-persona")
    (bot.parent / "persona.md").unlink()

    with pytest.raises(
        desired_state.DesiredStateError,
        match=r"BotSpec validation failed:.*prompts\.persona",
    ):
        desired_state.resolve_desired_services((bot,))


def test_resolver_runs_provider_endpoint_validation(tmp_path: Path) -> None:
    bot = _write_bot(
        tmp_path,
        "invalid-endpoint",
        providers=(
            "- id: local\n"
            "  kind: searxng\n"
            "  endpoint: http://searxng:8080\n"
            "  enabled: true"
        ),
    )

    with pytest.raises(
        desired_state.DesiredStateError,
        match=r"BotSpec validation failed:.*literal loopback host",
    ):
        desired_state.resolve_desired_services((bot,))


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker-calls.log"
    executable = bin_dir / "docker"
    executable.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$DOCKER_CALL_LOG"\n',
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log


def _run_services(
    tmp_path: Path,
    bot: Path,
    *args: str,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir, log = _fake_docker(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "CHATCOPILOT_BOT_SPECS": str(bot),
            "DOCKER_CALL_LOG": str(log),
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        }
    )
    env.pop("CHATCOPILOT_BOT_SPEC", None)
    result = subprocess.run(
        ("bash", str(_SCRIPT_PATH), *args),
        cwd=_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return result, log


def test_start_without_names_reconciles_only_desired_services(tmp_path: Path) -> None:
    bot = _write_bot(
        tmp_path,
        "active-bot",
        providers="- id: local\n  kind: searxng\n  enabled: true",
        mcp_servers="servers:\n  - ref: playwright-browser\n    enabled: true\n",
    )

    result, log = _run_services(tmp_path, bot, "start")

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert any("up -d searxng playwright-mcp" in call for call in calls)
    assert any("stop xiaohongshu-mcp" in call for call in calls)
    assert not any("up -d xiaohongshu-mcp" in call for call in calls)


def test_invalid_desired_state_never_calls_docker(tmp_path: Path) -> None:
    bot = _write_bot(
        tmp_path,
        "invalid-bot",
        providers="- id: local\n  kind: searxng\n  enabled: maybe",
    )

    result, log = _run_services(tmp_path, bot, "start")

    assert result.returncode != 0
    assert "desired-state" in result.stderr
    assert not log.exists()


def test_doctor_all_skips_every_disabled_service(tmp_path: Path) -> None:
    bot = _write_bot(tmp_path, "disabled-bot")

    result, log = _run_services(tmp_path, bot, "doctor", "all")

    assert result.returncode == 0, result.stderr
    assert "No desired Docker services to diagnose" in result.stdout
    assert not log.exists()


def test_explicit_start_remains_one_off_and_does_not_resolve_desired_state(tmp_path: Path) -> None:
    bot = _write_bot(tmp_path, "disabled-bot")

    result, log = _run_services(tmp_path, bot, "start", "xiaohongshu-mcp")

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert any("up -d xiaohongshu-mcp" in call for call in calls)
    assert not any("stop searxng" in call for call in calls)


def test_service_script_prefers_repo_venv_then_checks_ruamel_fallback() -> None:
    source = _SCRIPT_PATH.read_text(encoding="utf-8")

    assert 'local repo_python="$REPO_ROOT/.venv/bin/python"' in source
    assert "python3 -c 'import ruamel.yaml'" in source
    assert "no containers were changed" in source
