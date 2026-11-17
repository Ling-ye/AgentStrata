"""Native import receipts, fixed baselines and explicit recovery without Windows."""
import json
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from console.control.applications import Applications
from console.control.application_imports import Imports
from studio import projects, runtime


@pytest.fixture
def importer(tmp_path, monkeypatch):
    apps = Applications(Path(__file__).resolve().parents[2], tmp_path / "studio", discover=lambda: [])
    manager = Imports(apps)
    monkeypatch.setattr(projects, "select_live2d", lambda: (tmp_path / "source", tmp_path / "assets"))
    from chatcopilot.core.source_snapshot import manifest_digest
    monkeypatch.setattr("chatcopilot.core.source_snapshot.source_manifest", lambda *_: {})
    monkeypatch.setattr(manager, "_selection", lambda source, assets: {
        "root": str(source), "assets": str(assets), "source_digest": manifest_digest({}), "artifacts_digest": "b"*64})
    monkeypatch.setattr(projects, "import_live2d", lambda *_: None)
    return manager


def test_success_replay_and_reserved_id(importer, monkeypatch):
    key = uuid4().hex
    assert importer.register(key)["state"] == "pending"
    with pytest.raises(ValueError, match="导入"):
        importer.apps.create(key, "desktop")
    importer.process(key)
    assert importer.status(key)["state"] == "imported"
    monkeypatch.setattr(projects, "select_live2d", lambda: pytest.fail("must not reopen"))
    importer.process(key)
    assert importer.register(key)["application_id"] == "studio:" + key


def test_other_request_adopts_single_active_import(importer):
    first, second = uuid4().hex, uuid4().hex
    importer.register(first)
    assert importer.register(second)["id"] == first
    with pytest.raises(KeyError):
        importer.status(second)


def test_cancel_is_known_and_requires_explicit_retry(importer, monkeypatch):
    key = uuid4().hex
    importer.register(key)
    def cancel():
        raise projects.SelectionCancelled("cancelled")
    monkeypatch.setattr(projects, "select_live2d", cancel)
    importer.process(key)
    assert importer.status(key)["state"] == "cancelled"
    assert importer.register(key)["state"] == "cancelled"
    importer.retry(key)
    assert importer.status(key)["state"] == "pending"


def test_unknown_selector_requires_closed_window_confirmation(importer, monkeypatch):
    key = uuid4().hex
    importer.register(key)
    def interrupted():
        raise projects.SelectionInterrupted("unknown outcome")
    monkeypatch.setattr(projects, "select_live2d", interrupted)
    importer.process(key)
    result = importer.status(key)
    assert result["state"] == "recovery_required" and result["requires_selection_closed"]
    with pytest.raises(ValueError, match="关闭"):
        importer.retry(key)
    assert importer.retry(key, selection_closed=True)["state"] == "pending"


def test_crashed_selecting_is_not_replayed(importer, monkeypatch):
    key = uuid4().hex
    importer.register(key)
    importer._update(key, "selecting")
    monkeypatch.setattr(projects, "select_live2d", lambda: pytest.fail("must not reopen"))
    assert importer.status(key)["state"] == "recovery_required"
    importer.process(key)
    assert importer.status(key)["state"] == "recovery_required"


def test_fixed_selection_survives_crash_and_rejects_version_change(importer, monkeypatch):
    key = uuid4().hex
    importer.register(key)
    selected = importer._selection(Path("/old/source"), Path("/old/assets"))
    importer._update(key, "importing", json.dumps(selected))
    assert importer.status(key)["requires_selection_closed"] is False
    monkeypatch.setattr(projects, "select_live2d", lambda: pytest.fail("must reuse source"))
    importer.retry(key)
    importer.process(key)
    assert importer.status(key)["state"] == "imported"

    other = uuid4().hex
    importer.register(other)
    importer._update(other, "importing", json.dumps(selected))
    importer.retry(other)
    monkeypatch.setattr(importer, "_selection", lambda *_: {**selected, "source_digest": "c"*64})
    importer.process(other)
    assert importer.status(other)["state"] == "failed"
    assert importer.apps.get("studio:" + other) is None


def test_concurrent_managers_do_not_reopen_selector(importer, monkeypatch):
    key = uuid4().hex
    importer.register(key)
    entered, release = threading.Event(), threading.Event()
    def select():
        entered.set()
        release.wait(5)
        return Path("/source"), Path("/assets")
    monkeypatch.setattr(projects, "select_live2d", select)
    thread = threading.Thread(target=importer.process, args=(key,))
    thread.start()
    try:
        assert entered.wait(2)
        other = Imports(importer.apps)
        assert other.status(key)["state"] == "selecting"
        other.process(key)
        assert other.status(key)["state"] == "selecting"
    finally:
        release.set()
        thread.join(5)
    assert importer.status(key)["state"] == "imported"


def test_broken_old_evidence_does_not_hide_conversation(importer, monkeypatch):
    app = importer.apps.create(uuid4().hex, "desktop")
    key = uuid4().hex
    importer.apps.submit(app["id"], key, "old task")
    importer.studio.update(key, "failed", task="missing", version="")
    def missing(*_):
        raise KeyError("old task absent")
    monkeypatch.setattr(runtime, "task_verification_id", missing)
    row = importer.apps.messages(app["id"])[0]
    assert row["read_only"] and not row["retryable"] and row["recovery_message"]
    with pytest.raises(ValueError, match="只读"):
        importer.apps.retry(app["id"], key)


def test_crash_after_project_insert_finishes_receipt(importer):
    key = uuid4().hex
    importer.register(key)
    selected = importer._selection(Path("/source"), Path("/assets"))
    importer._update(key, "importing", json.dumps(selected))
    importer.studio.create(key, live=True, selection=(Path("/source"), Path("/assets")))
    assert importer.status(key)["state"] == "imported"
    assert importer._row(key)["state"] == "imported"
    other = uuid4().hex
    assert importer.register(other)["id"] == other


def test_http_import_status_rejects_browser_paths(importer, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from console.backend.routes.applications import router
    app = FastAPI()
    app.state.applications = importer.apps
    app.include_router(router)
    monkeypatch.setattr(Imports, "process", lambda *_: None)
    key = uuid4().hex
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/applications/imports/live2d", json={"id": key})
        assert response.status_code == 200 and response.json()["state"] == "pending"
        assert client.get("/api/applications/imports/" + key).json()["id"] == key
        assert client.post("/api/applications/imports/live2d", json={"id": key, "root": "/arbitrary"}).status_code == 422
        assert client.post("/api/applications", json={"id": uuid4().hex, "type": "live2d"}).status_code == 400


def test_another_process_holds_import_ownership(importer, monkeypatch):
    import subprocess
    import sys
    key = uuid4().hex
    importer.register(key)
    importer._update(key, "selecting")
    lock = importer.apps.state_dir / ("live2d-import-" + key + ".lock")
    code = ("from pathlib import Path; from chatcopilot.core.private_sqlite import private_lock; "
            "import sys; lock=private_lock(Path(sys.argv[1])); lock.__enter__(); "
            "print('locked', flush=True); sys.stdin.readline()")
    process = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        monkeypatch.setattr(projects, "select_live2d", lambda: pytest.fail("must not reopen"))
        assert importer.status(key)["state"] == "selecting"
        importer.process(key)
    finally:
        process.communicate("\n", timeout=5)
    assert importer.status(key)["state"] == "recovery_required"
    with pytest.raises(ValueError):
        importer.retry(key)


def test_partial_initial_rejects_extra_deployable_source(tmp_path, monkeypatch):
    import hashlib
    from chatcopilot.core.source_snapshot import manifest_digest
    source, parent, assets = tmp_path / "source", tmp_path / "project", tmp_path / "assets"
    (source / "src").mkdir(parents=True)
    original = b"VALUE = 1\n"
    (source / "src/example.py").write_bytes(original)
    manifest = {"src/example.py": {"sha256": hashlib.sha256(original).hexdigest(), "executable": False}}
    package = {"bot_id": "live2d-desktop", "source_manifest": manifest, "artifacts_digest": "b" * 64}
    installed = {"bot_id": "live2d-desktop", "platform": "windows-x86_64",
                 "artifacts_digest": package["artifacts_digest"], "delivered_digest": manifest_digest(manifest)}
    (source / ".agentstrata-application.json").write_text(json.dumps(installed))
    (source / ".agentstrata-source.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("chatcopilot.deployment.application_bundle.read_bundle", lambda *_a, **_kw: package)
    initial = projects.import_live2d(parent, source, assets)
    extra = initial / "src/extra.py"
    extra.write_text("UNEXPECTED = True\n")
    with pytest.raises(ValueError, match="额外"):
        projects.import_live2d(parent, source, assets)
    assert extra.read_text() == "UNEXPECTED = True\n"
    assert (initial / "src/example.py").read_bytes() == original
