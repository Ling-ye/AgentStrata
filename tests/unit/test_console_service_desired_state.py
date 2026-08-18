from __future__ import annotations

import subprocess
import textwrap

from console.control import operations, services
from console.control.instances import BotInstance


def test_service_catalog_matches_retained_runtime_boundaries() -> None:
    by_id = {service.id: service for service in services.SERVICES}

    assert set(by_id) == {
        "xiaohongshu",
        "searxng",
        "playwright",
        "napcat",
        "github",
    }
    assert by_id["searxng"].container == "chatcopilot-searxng"
    assert by_id["searxng"].compose_service == "searxng"
    assert by_id["searxng"].mcp_refs == ()
    assert by_id["searxng"].search_provider_kinds == ("searxng",)
    assert by_id["playwright"].container == "chatcopilot-playwright-mcp"
    assert by_id["playwright"].compose_service == "playwright-mcp"
    assert by_id["playwright"].mcp_refs == ("playwright-browser",)
    assert by_id["napcat"].has_doctor is True
    assert services._DOCTOR_TARGETS == {
        "xiaohongshu": "xhs",
        "searxng": "searxng",
        "playwright": "playwright",
    }


def test_napcat_doctor_runs_external_platform_check_via_gateway_status(
    monkeypatch,
) -> None:
    napcat = services.find_service("napcat")
    assert napcat is not None
    captured: dict[str, object] = {}

    def command(args: list[str], *, intro: str):
        captured["args"] = args
        captured["intro"] = intro
        yield "external-check fixture"
        yield "__EXIT__ 0"

    monkeypatch.setattr(services, "_command_streaming", command)

    output = list(services.doctor_streaming(napcat, "example-instance"))

    assert captured["args"][-3:] == ["status", "--instance", "example-instance"]
    assert captured["intro"] == "[external-check] NapCat QQ Gateway: example-instance"
    assert output == ["external-check fixture", "__EXIT__ 0"]


def test_compose_up_all_delegates_to_desired_state_reconcile(
    tmp_path,
    monkeypatch,
) -> None:
    script = tmp_path / "deploy" / "docker" / "services.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args, **kwargs):
        calls.append((list(args), dict(kwargs)))
        return subprocess.CompletedProcess(args, 0, "reconciled\n", "")

    monkeypatch.setattr(services, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(services.subprocess, "run", run)

    result = services.compose_up_all()

    assert result == {"ok": True, "stdout": "reconciled", "stderr": ""}
    assert calls == [
        (
            ["bash", str(script), "start"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 300.0,
            },
        )
    ]


def test_bot_enabled_services_includes_enabled_searxng_provider(
    tmp_path,
    monkeypatch,
) -> None:
    bot_dir = tmp_path / "bots" / "demo"
    (bot_dir / "mcp").mkdir(parents=True)
    (bot_dir / "mcp" / "servers.yaml").write_text(
        textwrap.dedent(
            """\
            servers:
              - ref: xiaohongshu-search
                enabled: "false"
              - ref: playwright-browser
                enabled: "yes"
            """
        ),
        encoding="utf-8",
    )
    (bot_dir / "bot.yaml").write_text(
        textwrap.dedent(
            """\
            id: demo
            platform:
              type: feishu
            tools:
              mcp:
                servers: mcp/servers.yaml
            agents:
              unified_search:
                enabled: true
                providers:
                  - id: tavily
                    kind: tavily
                    enabled: false
                  - id: searxng
                    kind: searxng
                    enabled: true
            """
        ),
        encoding="utf-8",
    )
    inst = BotInstance(
        instance_id="demo",
        bot_spec="bots/demo/bot.yaml",
        platform="feishu",
    )
    monkeypatch.setattr(operations, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        services,
        "all_services_status",
        lambda: [
            {
                "id": "searxng",
                "state": "healthy",
                "color": "green",
                "container": "chatcopilot-searxng",
            },
            {
                "id": "playwright",
                "state": "healthy",
                "color": "green",
                "container": "chatcopilot-playwright-mcp",
            },
        ],
    )

    enabled = operations.bot_enabled_services(inst)

    assert [item["service_id"] for item in enabled] == ["searxng", "playwright"]
    assert enabled[0]["reasons"] == ["Search provider: searxng"]
    assert enabled[1]["reasons"] == ["MCP: playwright-browser"]


def test_search_provider_projection_is_fail_closed() -> None:
    assert operations._config_enabled("yes", default=False) is True
    assert operations._config_enabled("off", default=True) is False
    assert operations._config_enabled("invalid", default=True) is False
    assert operations._enabled_search_provider_kinds({}) == set()
    assert operations._enabled_search_provider_kinds(
        {
            "agents": {
                "unified_search": {
                    "enabled": False,
                    "providers": [
                        {"id": "searxng", "kind": "searxng", "enabled": True}
                    ],
                }
            }
        }
    ) == set()
    assert operations._enabled_search_provider_kinds(
        {
            "agents": {
                "unified_search": {
                    "enabled": True,
                    "providers": [
                        {"id": "default-enabled", "kind": "tavily"},
                        {"id": "disabled", "kind": "brave", "enabled": False},
                        {"id": "string-enabled", "kind": "brave", "enabled": "on"},
                        {"id": "invalid-flag", "kind": "searxng", "enabled": "invalid"},
                        {"id": "enabled", "kind": "searxng", "enabled": True},
                    ],
                }
            }
        }
    ) == {"tavily", "brave", "searxng"}
