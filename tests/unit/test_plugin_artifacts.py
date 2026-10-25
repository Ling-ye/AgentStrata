from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tomllib

from integrations.open_llm_vtuber.agentstrata_vtuber.resources import artifact_lock, manifest, resource
from chatcopilot.plugins.catalog import PLUGINS


def test_plugin_catalog_package_and_resource_locks_agree():
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "integrations/open_llm_vtuber/pyproject.toml").read_text())["project"]
    entry = PLUGINS["open-llm-vtuber"]
    assert entry.version == project["version"] == "0.4.0"
    assert project["requires-python"] == ">=3.12,<3.13"
    assert entry.python == "3.12.14"
    lock = artifact_lock()
    for name, pin in manifest().items():
        assert pin["revision"] in lock[name]["url"]
        assert resource(pin["patch"]).is_file()
        assert re.fullmatch("[a-f0-9]{40}", pin["revision"])
    items = [lock[k] for k in ("node", "ffmpeg", "backend", "frontend")] + lock["asr"]
    for item in items:
        assert item["url"].startswith("https://") and "latest" not in item["url"]
        assert re.fullmatch("[a-f0-9]{64}", item["sha256"])
    requirements = resource("requirements.lock").read_text().replace("\\\n", " ")
    for line in requirements.splitlines():
        if line and not line.startswith(("#", "--")):
            assert ("==" in line or " @ https://" in line) and "--hash=sha256:" in line
    assert (root / "requirements/release-build.txt").read_bytes() == resource("build.lock").read_bytes()


def test_plugin_carries_original_legal_materials_and_relative_connection_patch():
    root = Path(__file__).resolve().parents[2]
    package = root / "integrations/open_llm_vtuber"
    for name in ("Open-LLM-VTuber-LICENSE.txt", "Open-LLM-VTuber-Web-LICENSE.txt"):
        assert (package / name).read_bytes() == (root / "third_party" / name).read_bytes()
    assert (package / "LICENSE").read_bytes() == (root / "LICENSE").read_bytes()
    patch = resource("patches/frontend.patch").read_text()
    assert "+const DEFAULT_BASE_URL = window.location.origin;" in patch
    assert "window.location.host" in patch
    data = json.loads(resource("config.example.json").read_text())
    assert data["gateway_token_env"] != data["mcp_token_env"]


def test_deployment_workspace_can_be_bound_by_the_existing_identity_flow(tmp_path):
    from integrations.open_llm_vtuber.agentstrata_vtuber.deployment import configure
    from chatcopilot.application.workspaces import build_actor_workspace
    from chatcopilot.contracts.authorization import Principal, stable_payload_digest
    from chatcopilot.contracts.identity import ConversationIdentity, Role
    from tests.model_settings_fixture import test_model_document

    root = tmp_path / "installation"
    backend = root / "runtime/apps/backend"
    templates = backend / "config_templates"
    templates.mkdir(parents=True)
    (templates / "conf.ZH.default.yaml").write_text(
        "system_config: {}\ncharacter_config:\n  agent_config: {}\n")
    (backend / "model_dict.json").write_text("[]")
    (backend / "live2d-models").mkdir()
    model = tmp_path / "model.json"
    model.write_text(json.dumps(test_model_document()))
    request = {"root": str(root), "host": {"python": sys.executable,
        "source_url": "https://example.com/source", "model_config": str(model), "model_binding": "chat"},
        "options": {"runtime": "native", "port": 19493, "gateway_port": 19494}}
    configure(request, {"backend": backend})
    workspace = root / "data/workspace"
    assert not list(workspace.iterdir())
    principal = Principal(channel="gateway-client", account_id="avatar-owner",
        conversation=ConversationIdentity("gateway-client", "p2p", "fixture-session"),
        user_id="avatar-owner", role=Role.OWNER, evidence_digest=stable_payload_digest({"fixture": True}))
    binding = build_actor_workspace(workspace_root=workspace, principal=principal)
    assert binding.workspace.root == workspace / "p2p_avatar-owner"
    assert (workspace / ".workspace-owner").is_file()
