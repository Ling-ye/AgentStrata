from __future__ import annotations

from pathlib import Path

import yaml


_ROOT = Path(__file__).resolve().parents[2]


def test_all_shared_mcp_published_ports_bind_loopback() -> None:
    compose = yaml.safe_load(
        (_ROOT / "deploy" / "docker" / "docker-compose.yaml").read_text(
            encoding="utf-8"
        )
    )
    published: list[str] = []
    for service in compose["services"].values():
        published.extend(str(binding) for binding in service.get("ports", ()))

    assert published
    assert all(binding.startswith("127.0.0.1:") for binding in published)
    searxng_mcp_env = compose["services"]["searxng-mcp"]["environment"]
    assert "searxng" in searxng_mcp_env["NO_PROXY"].split(",")
    assert searxng_mcp_env["no_proxy"] == searxng_mcp_env["NO_PROXY"]
    health_command = " ".join(
        compose["services"]["searxng-mcp"]["healthcheck"]["test"]
    )
    assert "HTTP_PROXY=" in health_command
    assert "http_proxy=" in health_command


def test_doctor_all_propagates_child_failures() -> None:
    script = (_ROOT / "deploy" / "docker" / "services.sh").read_text(
        encoding="utf-8"
    )

    assert "doctor_rc=0" in script
    assert "doctor_rc=1" in script
    assert 'bash "$0" doctor "$svc"' in script
    assert 'exit "$doctor_rc"' in script
    assert '"$0" doctor "$svc" || true' not in script
