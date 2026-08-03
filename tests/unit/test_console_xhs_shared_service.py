from __future__ import annotations

import json
from pathlib import Path

import pytest

from console.control import operations
from console.control.instances import BotInstance

ROOT = Path(__file__).resolve().parents[2]
XHS_IMAGE = (
    "xpzouying/xiaohongshu-mcp:v1.2.6@"
    "sha256:59fa30292e0c994cb2267c2d16b4ec119af800287a735c7386de1cd9c755bc6d"
)


def _write_bot(tmp_path: Path, servers_yaml: str) -> BotInstance:
    bot_dir = tmp_path / "bots" / "demo-bot"
    (bot_dir / "mcp").mkdir(parents=True)
    (bot_dir / "mcp" / "servers.yaml").write_text(servers_yaml, encoding="utf-8")
    (bot_dir / "bot.yaml").write_text(
        "\n".join(
            [
                "id: demo-bot",
                "display_name: Demo",
                "platform:",
                "  type: qq",
                "  adapter: qq_acp",
                "prompts:",
                "  persona: prompts/persona.md",
                "llm:",
                "  env_prefix: CHATCOPILOT_DEMO",
                "tools:",
                "  mcp:",
                "    servers: mcp/servers.yaml",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return BotInstance(
        instance_id="demo-bot",
        bot_spec="bots/demo-bot/bot.yaml",
        display_name="Demo",
        platform="qq",
    )


def test_xhs_compose_uses_pinned_official_image_without_local_derivative_build() -> None:
    compose = (ROOT / "deploy/docker/docker-compose.yaml").read_text(encoding="utf-8")
    xhs_service = compose.split("  xiaohongshu-mcp:\n", 1)[1].split(
        "\n  # ---------- Playwright",
        1,
    )[0]

    assert f"image: {XHS_IMAGE}" in xhs_service
    assert "build:" not in xhs_service
    assert "./xiaohongshu/" not in xhs_service
    for relative in (
        "Dockerfile",
        "crashless-cloak-chromium.sh",
        "mcp_server.search_only.go",
        "routes.search_only.go",
        "search_feeds_stability.patch",
    ):
        assert not (ROOT / "deploy/docker/xiaohongshu" / relative).exists()


def test_provision_schema_includes_xhs_login_when_ref_enabled(tmp_path: Path, monkeypatch) -> None:
    inst = _write_bot(tmp_path, "servers:\n  - ref: xiaohongshu-search\n    enabled: true\n")
    monkeypatch.setattr(operations, "repo_root", lambda: tmp_path)

    schema = operations.provision_schema(inst)

    assert schema["shared_services"] == [
        {
            "id": "xhs-login",
            "service": "xhs",
            "label": "小红书登录",
            "description": "启动小红书 MCP 并扫码登录，登录成功后继续部署。",
            "required": True,
        }
    ]


def test_provision_schema_omits_xhs_login_when_ref_disabled(tmp_path: Path, monkeypatch) -> None:
    inst = _write_bot(tmp_path, "servers:\n  - ref: xiaohongshu-search\n    enabled: false\n")
    monkeypatch.setattr(operations, "repo_root", lambda: tmp_path)

    schema = operations.provision_schema(inst)

    assert schema["shared_services"] == []


def test_extract_xhs_qrcode_from_mcp_image_content() -> None:
    raw = json.dumps(
        {
            "result": {
                "content": [
                    {
                        "type": "image",
                        "mimeType": "image/png",
                        "data": "ZmFrZS1wbmc=",
                    }
                ]
            }
        }
    )

    assert operations._extract_xhs_qrcode_data_url(raw) == "data:image/png;base64,ZmFrZS1wbmc="


def test_extract_xhs_qrcode_from_text_data_url() -> None:
    raw = json.dumps(
        {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "scan data:image/png;base64,ZmFrZQ== now",
                    }
                ]
            }
        }
    )

    assert operations._extract_xhs_qrcode_data_url(raw) == "data:image/png;base64,ZmFrZQ=="


def test_extract_xhs_qrcode_raises_on_mcp_error() -> None:
    raw = json.dumps({"error": {"code": -32000, "message": "login tool failed"}})

    with pytest.raises(ValueError, match="login tool failed"):
        operations._extract_xhs_qrcode_data_url(raw)


def test_xhs_check_login_connection_refused_returns_clear_error(monkeypatch) -> None:
    def fail_call(*_args, **_kwargs):
        raise ConnectionError("[Errno 111] Connection refused")

    monkeypatch.setattr(operations, "_xhs_mcp_call", fail_call)

    res = operations.shared_service_xhs_check_login()

    assert res["ok"] is False
    assert res["logged_in"] is False
    assert res["error"] == operations._XHS_MCP_NOT_RUNNING_MESSAGE
    assert "Errno 111" not in res["error"]


def test_xhs_qrcode_starts_service_after_connection_refused(monkeypatch) -> None:
    raw = json.dumps({"result": {"content": [{"type": "image", "mimeType": "image/png", "data": "ZmFrZS1wbmc="}]}})
    calls = []
    starts = []

    def mcp_call(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("[Errno 111] Connection refused")
        return raw

    def start_service():
        starts.append(1)
        return {"ok": True, "container": "chatcopilot-xiaohongshu-mcp", "stdout": "", "stderr": ""}

    monkeypatch.setattr(operations, "_xhs_mcp_call", mcp_call)
    monkeypatch.setattr(operations, "shared_service_xhs_start", start_service)

    res = operations.shared_service_xhs_login_qrcode()

    assert res["ok"] is True
    assert res["started"] is True
    assert res["image_data_url"] == "data:image/png;base64,ZmFrZS1wbmc="
    assert len(calls) == 2
    assert starts == [1]
