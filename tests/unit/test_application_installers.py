"""Installation is app-scoped, frozen and independently checked when downloaded."""
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import tarfile
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from chatcopilot.core.source_snapshot import git_output, source_manifest
from console.backend.routes.application_records import router
from console.control import application_installers as catalog
from console.control.applications import Applications
from studio import installer
from studio.installer import containers


def public_site(root, bot):
    path = root / "integrations" / bot / "index.html"
    path.parent.mkdir(parents=True)
    path.write_text("first public release")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return path


def read_setup(path):
    raw = path.read_bytes().split(b"\n__APPLICATION_PAYLOAD__\n", 1)[1]
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as files:
        return {item.name: files.extractfile(item).read() for item in files.getmembers() if item.isfile()}


def test_commit_build_via_api_is_immutable_app_bound_and_retryable(tmp_path, monkeypatch):
    apps = Applications(Path(__file__).resolve().parents[2], tmp_path / "studio", discover=lambda: [])
    project = uuid4().hex
    studio = apps.studio()
    studio.create(project)
    bot = "app-" + project
    root = studio.directory(project) / "initial"
    page = public_site(root, bot)
    directory = root / "bots" / bot
    directory.mkdir(parents=True)
    (directory / "agent.yaml").write_text("agents: {runtime: langgraph}\n")
    (directory / "bot.yaml").write_text(f"id: {bot}\nagent_profile: agent.yaml\n")
    git_output(root, "add", ".")
    git_output(root, "-c", "user.email=test@example.invalid", "-c", "user.name=Fixture", "commit", "-qm", "public")
    revision = git_output(root, "rev-parse", "HEAD")
    page.write_text("changed after commit")
    (page.parent / "local.env").write_text("PRIVATE=receiver-must-not-get-this")
    (page.parent / "untracked.txt").write_text("not committed")
    with studio.db.connect(write=True) as db:
        db.execute("UPDATE projects SET bot=?,version='initial' WHERE id=?", (bot, project))
        db.execute("INSERT INTO first_plans(project,requested_name,icon,target,approved) VALUES (?,?,?,'web',1)",
                   (project, "Sample", "book"))
    api = FastAPI()
    api.include_router(router)
    api.state.applications = apps
    url = "/api/applications/studio:" + project + "/installers"
    with TestClient(api, client=("127.0.0.1", 1), base_url="http://localhost") as http:
        info = http.get(url).json()
        assert info["commit"] == revision and info["builds"] == [] and not info["docker"]
        body = {"platform": "linux-x64", "kind": "commit", "revision": revision}
        assert http.post(url, json={**body, "revision": "HEAD"}).status_code == 400
        key = http.post(url, json=body).json()["id"]
        assert http.post(url, json=body).json()["id"] == key
        first, = http.get(url).json()["builds"]
        assert first["state"] == "ready" and first["origin"] == {"kind": "commit", "revision": revision}
        download = url + "/" + key + "/linux-x64"
        raw = http.get(download)
        assert raw.status_code == 200 and first["sha256"] == sha256(raw.content).hexdigest()
        assert http.get(download.replace("linux-x64", "windows-x64")).status_code == 404
        other = uuid4().hex
        studio.create(other)
        assert http.get(download.replace(project, other)).status_code == 404
        file = catalog.storage(apps, "studio:" + project) / key[17:] / (first["sha256"] + ".run")
        files = read_setup(file)
        assert files["site/index.html"] == b"first public release"
        assert first["components"] == ["网页及静态资源 · site/index.html"]
        receiver = tmp_path / "receiver"
        import os
        subprocess.run(["bash", str(file)], check=True,
                       env={**os.environ, "XDG_DATA_HOME": str(receiver), "DISPLAY": "", "WAYLAND_DISPLAY": ""})
        receipt = json.loads((receiver / "agentstrata/apps" / bot / "receipts" / (first["sha256"] + ".json")).read_text())
        assert http.post(download + "/installation", json={**receipt, "application": "other"}).status_code == 400
        assert http.post(download + "/installation", json={**receipt, "sha256": "0" * 64}).status_code == 404
        assert http.post(download + "/installation", json=receipt).status_code == 200
        verified, = http.get(url).json()["builds"]
        assert verified["installation"] == {"completed_at": receipt["completed_at"], "services_ready": False}
        assert not any("local.env" in f or "untracked" in f for f in files)
        file.write_bytes(b"changed")
        assert http.get(download).status_code == 400
        assert http.post(url, json=body).json()["id"] == key
        restored = http.get(download)
        assert restored.status_code == 200

        # A concurrent submission must not start a second builder for the same application and version.
        from chatcopilot.core.private_sqlite import private_lock
        with private_lock(file.parent / "lock"):
            assert http.post(url, json=body).json()["id"] == key
            assert http.get(download).content == restored.content

        # A packager upgrade may fail; the last published bytes stay available during and after the attempt.
        monkeypatch.setattr(installer, "recipe_digest", lambda: "new-recipe")
        def fail(*args, **kwargs):
            raise ValueError("missing build input")
        monkeypatch.setattr(installer, "build", fail)
        assert http.post(url, json=body).json()["id"] == key
        states = http.get(url).json()["builds"]
        assert [r["state"] for r in states] == ["failed", "ready"]
        assert http.get(download).content == restored.content


def test_gateway_receiver_separates_chat_and_worker_profiles(tmp_path, monkeypatch):
    from dataclasses import replace
    from chatcopilot.botspec.runtime import load_runtime_context
    from chatcopilot.core.config import load_config
    from chatcopilot.core.model_settings import ModelSettingsStore
    from studio.installer import gateway

    root = Path(__file__).resolve().parents[2]
    runtime = replace(load_runtime_context(root / "bots/lingye-copilot-qq/bot.yaml"), runtime_id="langgraph")
    answers = iter(("ws://127.0.0.1:3001", "900000001", "onebot-fixture-token-0123456789abcdef",
                    "900000002", "https://example.invalid/v1", "fixture-chat", "fixture-key", "fixture-worker"))
    monkeypatch.setattr(gateway, "DATA", tmp_path)
    monkeypatch.setattr(gateway, "prompt", lambda *a, **kw: next(answers))
    monkeypatch.setattr(gateway, "resolve_codex_executable", lambda _: Path("/fixture/codex"))
    state = tmp_path / "receiver.json"
    gateway.configure(runtime, state)
    env = json.loads(state.read_text())
    settings = ModelSettingsStore(Path(env["AGENTSTRATA_LLM_CONFIG"])).read()
    assert settings["connections"]["code-worker"]["auth"] == {"mode": "chatgpt", "profile": "worker"}
    assert settings["bindings"][runtime.spec.llm.chat.binding] == "receiver"
    assert load_config(environment=env).routing.code_task_profile == "code-worker"
    assert env["CHATCOPILOT_ADD_OWNER_IDS"] == "900000002"


def test_candidate_cannot_be_selected_from_another_application():
    host = SimpleNamespace(summary=lambda _: {"source": {"bot_id": "other"}})
    with pytest.raises(KeyError):
        catalog.task_controller(None, {"source": "repository", "instance_id": "mine"}, "repair-a", lambda: host)


def test_accepted_web_resources_have_no_docker_dependency(tmp_path):
    root = tmp_path / "source"
    public_site(root, "my-site")
    (root / "framework.txt").write_text("not an application asset")
    candidate = {"kind": "candidate", "task": "repair-sample", "revision": "b" * 64}
    info = installer.build(root, tmp_path / "out", name="网页", bot="my-site", kind="web", manifest=source_manifest(root),
                           origin=candidate, targets=["linux-x64"])
    files = read_setup(tmp_path / "out/application.run")
    assert set(files) == {"install.json", "site/index.html"}
    assert json.loads(files["install.json"])["origin"] == candidate
    assert info["docker"] is False and set(info["installers"]) == {"linux-x64"}


@pytest.mark.parametrize("invalid", ["volumes: [/home:/data]", "privileged: true", "environment: [HOME]",
                                     "environment: {SECRET: '${PRIVATE}'}", "network_mode: host",
                                     "image: 'redis:latest'", "build: ../other"])
def test_service_validation_rejects_host_resources_before_build(tmp_path, monkeypatch, invalid):
    root = tmp_path / "source"
    base = public_site(root, "my-service").parent
    (base / "install.json").write_text("{}")
    (base / "compose.yaml").write_text(
        "services:\n  app:\n    healthcheck: {test: [CMD, /probe]}\n    " + invalid + "\n")
    monkeypatch.setattr(containers, "docker", lambda *a, **kw: pytest.fail("Docker ran before validation"))
    with pytest.raises(ValueError):
        installer.build(root, tmp_path / "stage", bot="my-service", kind="source", name="Service",
                        origin={"kind": "commit", "revision": "a" * 40}, manifest=source_manifest(root), targets=["linux-x64"])


def test_interrupted_job_is_visible_and_never_becomes_downloadable(tmp_path):
    class Apps:
        state_dir = tmp_path
    row = {"source": "plugin", "type": "companion", "id": "plugin:a"}
    directory = catalog.storage(Apps, row["id"]) / ("b" * 64)
    directory.mkdir(mode=0o700, parents=True)
    (directory / "state.json").write_text(json.dumps({"state": "building", "updated": 1}))
    assert catalog.catalog(Apps, row)["builds"][0]["state"] == "failed"
    with pytest.raises(KeyError):
        catalog.download(Apps, row, catalog.public_id(row["id"], "b" * 64), "windows-x64")


def test_local_client_and_helper_use_single_install_without_docker(tmp_path):
    root = tmp_path / "source"
    area = public_site(root, "local-client").parent
    (area / "install.json").write_text(json.dumps({"clients": {"linux-x64": "app"}}))
    (area / "app").write_text("#!/bin/sh\nexec ./helper\n")
    (area / "helper").write_text("#!/bin/sh\necho OK\n")
    (area / "app").chmod(0o755)
    (area / "helper").chmod(0o755)
    out = tmp_path / "output"
    record = installer.build(root, out, bot="local-client", kind="source", name="Local client",
                             origin={"kind": "commit", "revision": "a"*40}, manifest=source_manifest(root), targets=None)
    assert not record["docker"] and list(record["installers"]) == ["linux-x64"]
    files = read_setup(out / "application.run")
    assert files["client-linux"].strip() == b"source/app" and "source/helper" in files
    descriptor = {"clients": {"windows-x64": "client/app.exe"}, "requirements": ["绑定账号"]}
    assert installer.layout("desktop", "local-client", descriptor, True) == {
        "docker": True, "platforms": ["windows-x64"], "clients": descriptor["clients"], "requirements": ["绑定账号"]}
