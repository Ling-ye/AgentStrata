"""Real Gateway processes load the selected source/profile; no model or QQ is called."""
from contextlib import nullcontext
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

from chatcopilot.core.release_identity import execution_identity
from chatcopilot.core.source_snapshot import copy_sources, manifest_digest, source_manifest
from chatcopilot.deployment.activation_runtime import InstalledReleaseRuntime
from chatcopilot.deployment.activation_service import ActivationService
from chatcopilot.contracts.deployment import ActivationTarget, ReleaseSource
from chatcopilot.plugins.runtime import UserServices
from chatcopilot.contracts.deployment import ServiceDefinition
from tests.application_fixture import write_application
from chatcopilot.protocols.gateway_client import GatewayClientConfig, GatewayWebSocketClient
from chatcopilot.contracts.gateway_rpc import SessionsCreateParams, SessionsGetParams

ROOT = Path(__file__).resolve().parents[2]


class LocalServices(UserServices):
    """Replace only systemd transport; the normal service command runs in a child."""
    def __init__(self, service, root, environment):
        self.service, self.root, self.environment = service, root, environment
        self.process = None
        self.log = (root / "process.log").open("wb")

    def command(self, action, *args):
        if action == "start":
            self.process = subprocess.Popen(self.version_command(self.service, self.root), cwd=self.root,
                env=self.environment, stdout=self.log, stderr=self.log)
        elif action == "stop":
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.process.wait(15)
        running = self.process is not None and self.process.poll() is None
        return SimpleNamespace(returncode=0, stdout=f"MainPID={self.process.pid if running else 0}\nActiveState={'active' if running else 'inactive'}\nSubState={'running' if running else 'dead'}\n")

    def close(self):
        self.command("stop")
        self.log.close()


def test_real_gateway_switch_and_withdraw_load_exact_profile_and_preserve_state(tmp_path, monkeypatch):
    baseline, candidate = tmp_path / "installed", tmp_path / "candidate"
    original = source_manifest(ROOT)
    copy_sources(ROOT, baseline, original)
    bot_relative = "bots/release-fixture/bot.yaml"
    app = baseline / "bots/release-fixture"
    (app / "prompts").mkdir(parents=True)
    (app / "prompts/identity.md").write_text("Baseline identity")
    (app / "prompts/style.md").write_text("Reply clearly.")
    write_application(app / "bot.yaml", {"id": "release-fixture", "display_name": "Release fixture",
        "gateway": {"clients": [{"id": "operator", "token_env": "RELEASE_GATEWAY_TOKEN", "role": "owner"}]},
        "llm": {"chat": {"binding": "release.chat"}, "research": {"binding": "release.chat"}},
        "agents": {"runtime": "langgraph"}, "prompts": {"schema_version": 2, "identity": "prompts/identity.md", "response_style": "prompts/style.md"},
        "tools": {"packs": []}})
    before = dict(original)
    for file in app.rglob("*"):
        if file.is_file():
            before[str(file.relative_to(baseline))] = {"sha256": hashlib.sha256(file.read_bytes()).hexdigest(), "executable": False}
    copy_sources(baseline, candidate, before)
    (candidate / "bots/release-fixture/prompts/identity.md").write_text("Accepted candidate identity")
    after = {**before, "bots/release-fixture/prompts/identity.md": {
        "sha256": hashlib.sha256(b"Accepted candidate identity").hexdigest(), "executable": False}}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    data = baseline / "data"
    data.mkdir(mode=0o700)
    for name in ("gateway", "workspace", "wiki"):
        (data / name).mkdir(mode=0o700)
    sentinel = data / "memory.json"
    sentinel.write_text('{"retained":true}')
    models = tmp_path / "model.json"
    models.write_text(json.dumps({"connections": {"fixture": {"kind": "openai_compatible",
        "base_url": "http://127.0.0.1:9/v1", "auth": {"mode": "api_key", "key_env": "RELEASE_MODEL_KEY"}}},
        "profiles": {"fixture": {"connection": "fixture", "model": "unused-model"}}, "bindings": {"release.chat": "fixture"}}))
    values = {"AGENTSTRATA_LLM_CONFIG": str(models), "RELEASE_MODEL_KEY": "synthetic-release-key",
        "RELEASE_GATEWAY_TOKEN": "s" * 48, "CHATCOPILOT_GATEWAY_PORT": str(port),
        "CHATCOPILOT_GATEWAY_STATE_ROOT": str(data / "gateway"), "CHATCOPILOT_WORKSPACE_ROOT": str(data / "workspace"),
        "CHATCOPILOT_WIKI_ROOT": str(data / "wiki"), "CHATCOPILOT_SOURCE_URL": "https://example.com/source"}
    env_file = baseline / "runtime.env"
    env_file.write_text("\n".join(key + "=" + json.dumps(value) for key, value in values.items()))
    env_file.chmod(0o600)
    environment = {**os.environ, **values, "PYTHONPATH": str(baseline / "src"), "PYTHONUNBUFFERED": "1"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    service = ServiceDefinition("gateway", (sys.executable, "-m", "chatcopilot", "run", "--bot", str(app / "bot.yaml")),
        str(baseline), str(env_file), port, versioned=True)
    target = ActivationTarget("instance:release-fixture", baseline, app / "bot.yaml", env_file, data / "gateway", (service,),
        {"chatcopilot": "src/chatcopilot"}, {"gateway": "fixture.service"})
    services = LocalServices(service, baseline, environment)
    runtime = InstalledReleaseRuntime(target, services=services)
    runtime.installation_lock = nullcontext
    facts = execution_identity(app / "bot.yaml", environment)
    source = ReleaseSource("repair-" + "a" * 32, "release-fixture", baseline, before, candidate, after,
        {"candidate_digest": manifest_digest(after), "goal_digest": "fixture", "verification_digest": "fixture", "review_binding": "fixture"},
        bot_relative, facts["environment"], facts["model"])
    manager = ActivationService(runtime, timeout=20)
    async def session(create=False):
        client = GatewayWebSocketClient(GatewayClientConfig(f"ws://127.0.0.1:{port}", "s" * 48,
            client_id="operator", client_mode="interactive"))
        try:
            await client.ensure_connected()
            if create:
                return await client.request("sessions.create", SessionsCreateParams("release-session"), idempotency_key="release-session")
            return await client.request("sessions.get", SessionsGetParams("release-session"))
        finally:
            await client.close()
    try:
        runtime.start()
        deadline = time.monotonic() + 20
        while True:
            try:
                first = runtime.observe()["gateway"]
                break
            except (RuntimeError, OSError):
                if time.monotonic() >= deadline:
                    raise AssertionError((baseline / "process.log").read_text())
                time.sleep(.1)
        asyncio.run(session(create=True))
        prepared = manager.prepare(source)
        result = manager.activate(prepared["id"])
        assert result["state"] == "active", (result, (baseline / "process.log").read_text())
        current = runtime.observe()["gateway"]
        assert current["process"] != first["process"]
        assert current["version"] == source.receipt["candidate_digest"]
        assert Path(current["module"]).is_relative_to(Path(result["candidate_root"]))
        assert current["configuration"] != first["configuration"]
        assert current["state_root"] == first["state_root"]
        assert asyncio.run(session()).session.session_id == "release-session"
        reverted = manager.rollback(result["id"])
        assert reverted["state"] == "rolled_back", reverted
        assert runtime.observe()["gateway"]["configuration"] == first["configuration"]
        assert sentinel.read_text() == '{"retained":true}'
        assert asyncio.run(session()).session.session_id == "release-session"
    finally:
        services.close()
