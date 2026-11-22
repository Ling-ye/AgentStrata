"""The guide must use the same effective model document as its Console host."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from studio import setup
from studio.console_server import application

ROOT = Path(setup.__file__).resolve().parents[1]


def settings(model="fixture-chat"):
    return {"connections": {"local": {"kind": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
            "auth": {"mode": "api_key", "key_env": "STUDIO_DEFAULTS_FIXTURE_KEY"}}},
            "profiles": {"chat": {"connection": "local", "model": model}}, "bindings": {"chat": "chat"}}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def guides(tmp_path, monkeypatch):
    from chatcopilot.harness import config
    monkeypatch.setattr(config, "configuration", lambda: {})
    processes = []
    created = []

    class Process:
        def __init__(self, args, kwargs):
            self.args, self.environment = args, kwargs["env"]
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            assert self.terminated and timeout == 30
            return 0

    def spawn(args, **kwargs):
        process = Process(args, kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(setup.subprocess, "Popen", spawn)
    monkeypatch.setattr(setup.Guide, "wait_for_gateway", lambda self, port: None)

    def create(home=None, *, model_path=None, legacy=False):
        home = home or tmp_path / "studio"
        home.mkdir(parents=True, exist_ok=True)
        guide = setup.Guide(home, ROOT, **({} if legacy else {"model_path": model_path or home / "models.json"}))
        created.append(guide)
        return guide

    yield SimpleNamespace(create=create, processes=processes)
    for guide in created:
        guide.stop()


def test_console_advertised_defaults_start_the_same_guide_without_saving(tmp_path, guides):
    home = tmp_path / "studio"
    default = home / "models.defaults.json"
    write(default, settings())
    original = default.read_bytes()
    app = application(home, ROOT, "fixture-token")
    app.state.applications._discover = lambda: []
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    try:
        assert client.post("/api/login", json={"token": "fixture-token"}).status_code == 200
        view = client.get("/api/llm/config").json()
        assert view["source"] == "defaults" and view["bindings"] == {"chat": "chat"}
        guide = guides.create(home, model_path=app.state.applications.model_path)
        guide.start()
        assert len(guides.processes) == 1
        assert guides.processes[0].environment["AGENTSTRATA_LLM_CONFIG"] == str(home / "models.json")
        assert default.read_bytes() == original and not (home / "models.json").exists()
    finally:
        client.close()


def test_effective_defaults_change_restarts_guide_once(tmp_path, guides):
    path = tmp_path / "studio/models.defaults.json"
    write(path, settings("first"))
    guide = guides.create()
    guide.start()
    assert len(guides.processes) == 1
    guide.start()
    assert len(guides.processes) == 1
    write(path, settings("second"))
    guide.start()
    assert len(guides.processes) == 2 and guides.processes[0].terminated
    assert not guides.processes[1].terminated


def test_saved_settings_win_and_unused_default_change_does_not_restart(tmp_path, guides):
    path = tmp_path / "studio/models.json"
    write(path, settings("saved"))
    defaults = path.with_suffix(".defaults.json")
    write(defaults, settings("default"))
    guide = guides.create()
    guide.start()
    write(defaults, settings("unused-new-default"))
    guide.start()
    assert len(guides.processes) == 1 and not guides.processes[0].terminated
    assert json.loads(path.read_bytes()) == settings("saved")


def test_removing_saved_settings_returns_to_defaults_without_copying(tmp_path, guides):
    path = tmp_path / "studio/models.json"
    write(path, settings("saved"))
    write(path.with_suffix(".defaults.json"), settings("default"))
    guide = guides.create()
    guide.start()
    path.unlink()
    guide.start()
    assert len(guides.processes) == 2 and guides.processes[0].terminated
    assert not path.exists()


def test_missing_saved_and_default_settings_do_not_launch_or_write(tmp_path, guides):
    guide = guides.create()
    before = list(guide.state_dir.iterdir())
    guide.start()
    assert not guides.processes and guide.process is None
    assert list(guide.state_dir.iterdir()) == before


def test_legacy_entry_does_not_adopt_an_unadvertised_default_document(tmp_path, guides):
    write(tmp_path / "studio/models.defaults.json", settings())
    guide = guides.create(legacy=True)
    guide.start()
    assert not guides.processes and not (guide.state_dir / "models.json").exists()


def test_non_file_saved_path_does_not_adopt_defaults(tmp_path, guides):
    path = tmp_path / "studio/models.json"
    path.mkdir(parents=True)
    write(path.with_suffix(".defaults.json"), settings())
    guide = guides.create()
    guide.start()
    assert not guides.processes and path.is_dir()


def test_explicit_shared_store_uses_its_defaults_not_the_studio_home(tmp_path, guides):
    shared = tmp_path / "selected/model.json"
    write(shared.with_suffix(".defaults.json"), settings("selected"))
    guide = guides.create(model_path=shared)
    guide.start()
    assert len(guides.processes) == 1
    assert guides.processes[0].environment["AGENTSTRATA_LLM_CONFIG"] == str(shared)
    assert not shared.exists() and not (guide.state_dir / "models.json").exists()
