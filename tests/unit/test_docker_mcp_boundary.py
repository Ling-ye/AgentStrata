from __future__ import annotations

from pathlib import Path

import yaml


_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_PATH = _ROOT / "deploy" / "docker" / "docker-compose.yaml"


def _compose() -> dict:
    return yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))


def test_compose_contains_only_reviewed_isolated_services() -> None:
    compose = _compose()

    assert set(compose["services"]) == {
        "searxng",
        "playwright-mcp",
        "xiaohongshu-mcp",
    }
    source = _COMPOSE_PATH.read_text(encoding="utf-8")
    for removed in (
        "tavily-mcp",
        "brave-search-mcp",
        "sequential-thinking-mcp",
        "searxng-mcp",
        "taoke-mcp",
    ):
        assert removed not in source


def test_retained_services_are_profiled_loopback_only_and_bounded() -> None:
    compose = _compose()
    services = compose["services"]

    for service in services.values():
        assert service["profiles"]
        assert service["pids_limit"] > 0
        assert service["mem_limit"]
        assert service["cpus"] > 0
        assert service["healthcheck"]["test"]
        assert service["logging"]["options"]["max-size"]
        assert service["logging"]["options"]["max-file"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert all(str(port).startswith("127.0.0.1:") for port in service["ports"])
        assert "@sha256:" in service["image"]

    assert services["searxng"]["networks"] == ["searxng-egress"]
    assert services["playwright-mcp"]["networks"] == ["playwright-egress"]
    assert services["xiaohongshu-mcp"]["networks"] == ["xiaohongshu-egress"]
    assert services["playwright-mcp"]["user"] == "1000:1000"
    assert services["playwright-mcp"]["cap_drop"] == ["ALL"]
    assert "--no-sandbox" not in services["playwright-mcp"]["command"]
    allowed_hosts_index = services["playwright-mcp"]["command"].index("--allowed-hosts")
    assert services["playwright-mcp"]["command"][allowed_hosts_index + 1] == (
        "127.0.0.1,localhost,127.0.0.1:18066,localhost:18066"
    )
    assert services["searxng"]["ports"] == ["127.0.0.1:18064:8080"]
    assert services["xiaohongshu-mcp"]["ports"] == ["127.0.0.1:18060:18060"]
    assert services["playwright-mcp"]["ports"] == ["127.0.0.1:18066:8931"]
    source = _COMPOSE_PATH.read_text(encoding="utf-8")
    for removed_override in (
        "XHS_MCP_PORT",
        "SEARXNG_PORT",
        "PLAYWRIGHT_MCP_PORT",
    ):
        assert removed_override not in source
    assert services["playwright-mcp"]["environment"] == {
        "HOME": "/tmp",
        "XDG_CACHE_HOME": "/tmp/playwright-cache",
        "XDG_CONFIG_HOME": "/tmp/playwright-config",
    }
    assert all(
        not str(mount).startswith("/home/")
        for mount in services["playwright-mcp"]["tmpfs"]
    )


def test_retained_images_use_approved_digests() -> None:
    services = _compose()["services"]

    assert services["searxng"]["image"].endswith(
        "sha256:f4c8e59de166ed71f6380c0847c312ca51f0d41996e31d0559163b6b09ecde52"
    )
    assert services["playwright-mcp"]["image"].endswith(
        "sha256:3108dac789720d5236ee1869ad65c8f32fbbfe9d7eea8a5eb89920ab35a665d6"
    )
    assert services["xiaohongshu-mcp"]["image"].endswith(
        "sha256:59fa30292e0c994cb2267c2d16b4ec119af800287a735c7386de1cd9c755bc6d"
    )
