"""Trusted build identity, explicit selection and fail-closed desktop launch."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.control import application_desktop as desktop
from console.control.applications import Applications
from console.backend.routes.application_records import router
from chatcopilot.core.source_snapshot import manifest_digest
from studio.delivery import digest


@pytest.fixture
def built(tmp_path, monkeypatch):
    root = tmp_path / "framework"
    script = root / "integrations/desktop_app/launch.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("trusted script")
    applications = Applications(root, tmp_path / "studio", discover=lambda: [])
    studio = applications.studio()
    project, message = uuid4().hex, uuid4().hex
    bot = "app-" + project
    with studio.db.connect(write=True) as db:
        db.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)", (project, "test", bot, "initial", None, 1))
        db.execute("INSERT INTO messages(id,project,text,state,build,updated) VALUES (?,?,?,'ready',?,?)",
                   (message, project, "build", message, 1))
    stage = studio.directory(project) / "builds" / message
    stage.mkdir(parents=True)
    installer = stage / "application.exe"
    installer.write_bytes(b"verified installer")
    manifest = {"schema": desktop.SCHEMA, "install_id": "app-trial",
                "executable": "app-trial.exe", "files": {"app-trial.exe": "a"*64, "resources/app.asar": "b"*64}}
    launch = stage / "desktop-launch.json"
    launch.write_text(json.dumps(manifest))
    sources = {"src/app.py": {"sha256": "c"*64, "executable": False}}
    payload = {"bot_id": bot, "bot_relative": "bots/" + bot + "/bot.yaml", "source_digest": manifest_digest(sources),
               "artifacts_digest": "d"*64, "source_manifest": sources}
    receipt = {**payload, "install_id": manifest["install_id"],
               "installer_sha256": digest(installer), "launcher_manifest_sha256": digest(launch)}
    receipt_path = stage / "build-receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    monkeypatch.setattr(desktop, "read_bundle", lambda path: payload)
    native = Mock(return_value={"executable": r"C:\\Apps\\app-trial.exe", "state": "bound"})
    monkeypatch.setattr(desktop, "_native", native)
    return SimpleNamespace(apps=applications, id="studio:"+project, message=message, native=native,
                           stage=stage, manifest=manifest, receipt=receipt, payload=payload, script=script)


def test_explicit_binding_and_launch_revalidate_exact_build(built):
    with pytest.raises(ValueError, match="Select and verify"):
        desktop.operate(built.apps, built.id, built.message, "launch")
    built.native.assert_not_called()
    assert desktop.operate(built.apps, built.id, built.message, "bind")["state"] == "bound"
    with pytest.raises(ValueError, match="runtime dependencies"):
        desktop.operate(built.apps, built.id, built.message, "launch")
    assert built.native.call_args.args[1] == "select"
    (built.stage / "application.exe").write_bytes(b"changed")
    with pytest.raises(ValueError, match="摘要"):
        desktop.operate(built.apps, built.id, built.message, "launch")
    assert built.native.call_count == 1


@pytest.mark.parametrize("change", ["old", "manifest", "source", "bot", "script"])
def test_missing_or_changed_trust_never_launches(built, change):
    if change == "script":
        desktop.operate(built.apps, built.id, built.message, "bind")
        built.native.reset_mock()
        built.script.write_text("changed trusted code; requires explicit rebinding")
    elif change == "old":
        built.receipt.pop("launcher_manifest_sha256")
        (built.stage / "build-receipt.json").write_text(json.dumps(built.receipt))
    elif change == "manifest":
        (built.stage / "desktop-launch.json").write_text("{}")
    elif change == "source":
        built.payload["source_digest"] = "e"*64
    else:
        built.payload["bot_id"] = "another-app"
    with pytest.raises(ValueError):
        desktop.operate(built.apps, built.id, built.message, "launch")
    built.native.assert_not_called()


@pytest.mark.parametrize("member", ["../evil.exe", "C:/evil.exe", "resources/../evil.exe", "app-trial.EXE"])
def test_unsafe_or_case_aliased_manifest_rejected(built, member):
    built.manifest["files"][member] = "a"*64
    path = built.stage / "desktop-launch.json"
    path.write_text(json.dumps(built.manifest))
    built.receipt["launcher_manifest_sha256"] = digest(path)
    (built.stage / "build-receipt.json").write_text(json.dumps(built.receipt))
    with pytest.raises(ValueError, match="member"):
        desktop.operate(built.apps, built.id, built.message, "bind")
    built.native.assert_not_called()


def test_cancelled_selection_does_not_register(built):
    built.native.side_effect = ValueError("Selection cancelled")
    with pytest.raises(ValueError):
        desktop.operate(built.apps, built.id, built.message, "bind")
    built.native.side_effect = None
    with pytest.raises(ValueError, match="Select and verify"):
        desktop.operate(built.apps, built.id, built.message, "launch")


def test_http_target_origin_and_command_injection_boundaries(built):
    app = FastAPI()
    app.state.applications = built.apps
    app.include_router(router)
    url = "/api/applications/" + built.id + "/builds/" + built.message + "/desktop/"
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        assert client.post(url+"bind", json={"executable": "evil.exe"}).status_code == 422
        assert client.post(url+"shell", json={}).status_code == 422
        assert client.post(url+"bind", json={}, headers={"Origin": "https://example.com"}).status_code == 403
        other = url.replace(built.id, "studio:"+uuid4().hex)
        assert client.post(other+"bind", json={}).status_code == 404
        assert client.post(url+"bind", json={}).status_code == 200
    assert built.native.call_count == 1
