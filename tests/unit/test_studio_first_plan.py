"""A Studio creation request is durable. Only a reviewed revision can start work."""

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.backend.routes.applications import router
from console.control.applications import Applications
from studio import first_plan, projects, runtime, project_archive
from studio.workspace import Studio

ROOT = Path(__file__).resolve().parents[2]


def test_draft_revision_confirmation_restart_and_web_baseline(tmp_path, monkeypatch):
    apps = Applications(ROOT, tmp_path / "studio", discover=lambda: [])
    app = FastAPI()
    app.include_router(router)
    app.state.applications = apps
    project = uuid4().hex
    draft = {"id": project, "requirement": "做一个网页报修台", "icon": "tools", "name": ""}
    url = "/api/applications/studio:" + project

    async def proposal(*args, planning):
        assert planning
        return {
            "action": "plan",
            "target": "web",
            "name": "网页报修台",
            "scope": "报修登记",
            "acceptance": "登记后在列表可见",
            "delivery": {"platforms": ["linux-x64"], "entry": "浏览器", "services": "无", "bindings": "无"},
            "reply": "请确认",
        }

    monkeypatch.setattr(runtime, "discuss", proposal)
    monkeypatch.setattr(projects, "project_inputs", lambda _: {})
    starts = []

    def start(*args, **kwargs):
        starts.append(args)
        return {"task_id": "repair-first"}

    monkeypatch.setattr(runtime, "start_task", start)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 4210)) as http:
        assert http.post("/api/applications/drafts", json=draft).json()["id"] == "studio:" + project
        assert http.post("/api/applications/drafts", json=draft).status_code == 200
        assert (
            http.post(
                "/api/applications/drafts", json={**draft, "requirement": "不同需求"}
            ).status_code
            == 400
        )
        assert (
            http.post(url + "/messages", json={"id": uuid4().hex, "text": "现在开工"}).status_code
            == 400
        )
        assert http.post(url + "/first-plan/confirm", json={"revision": 1}).status_code == 400
        studio = Studio(apps.state_dir, ROOT)
        studio.step(project)
        plan = http.get(url + "/first-plan").json()
        assert (plan["state"], plan["target"], studio.pending()) == ("plan_review", "web", [])
        assert not (studio.directory(project) / "initial").exists()
        edit = {
            "revision": 1,
            "name": plan["name"],
            "scope": "登记、处理",
            "acceptance": "刷新后仍为完成",
            "delivery": {**plan["delivery"], "entry": "应用首页"},
            "target": "web",
        }
        assert http.post(url + "/first-plan/edit", json={**edit, "target": "desktop"}).status_code == 400
        assert http.post(url + "/first-plan/edit", json={**edit, "delivery": {**edit["delivery"], "entry": ""}}).status_code >= 400
        assert http.post(url + "/first-plan/edit", json=edit).json()["revision"] == 2
        assert http.post(url + "/first-plan/confirm", json={"revision": 1}).status_code == 400
        for _ in range(2):
            assert http.post(url + "/first-plan/confirm", json={"revision": 2}).status_code == 200
        assert not starts
        studio = Studio(apps.state_dir, ROOT)
        studio.step(project)
        changed = first_plan.read(studio, project)
        assert changed["state"] == "starting", changed
        assert changed["delivery"]["entry"] == "应用首页"
        source = studio.directory(project) / "initial"
        assert (source / "integrations" / ("app-" + project) / "index.html").is_file()
        from chatcopilot.core.source_snapshot import source_manifest
        from chatcopilot.deployment.packaging import read_plan
        standalone = read_plan(source, source_manifest(source), "app-" + project)
        assert standalone.recipe == "static" and standalone.platforms == ("linux-x64",)
        assert not (source / "bots/desktop-reader").exists()
        from chatcopilot.botspec.runtime import load_runtime_context
        assert load_runtime_context(source / "bots" / ("app-" + project) / "bot.yaml").spec.llm.research.binding == "chat"
        assert not starts
        studio.step(project)
        assert len(starts) == 1 and "刷新后仍为完成" in starts[0][3]
        assert "linux-x64" in starts[0][3] and "应用首页" in starts[0][3]
        assert http.get(url).json()["type"] == "web"


def test_later_request_keeps_service_delivery_and_previous_plan_record(tmp_path, monkeypatch):
    import json
    project, message = uuid4().hex, uuid4().hex
    studio = Studio(tmp_path, ROOT)
    first_plan.create(studio, project, "报修台", "tools", "以网页提交工单")
    delivery = {"platforms": ["linux-x64"], "entry": "浏览器", "services": "Docker 和数据卷", "bindings": "无"}
    async def discuss(_home, _project, _request, context, *, planning=False):
        if planning:
            return {"action": "plan", "name": "报修台", "target": "source", "scope": "登记工单",
                    "acceptance": "重启后能找回", "delivery": delivery, "reply": "确认"}
        data = json.loads(context)
        assert data["application_type"] == "source" and data["delivery"] == delivery
        return {"action": "develop", "name": "报修台", "reply": "继续"}
    monkeypatch.setattr(runtime, "discuss", discuss)
    monkeypatch.setattr(projects, "project_inputs", lambda _: {})
    studio.step(project)
    first_plan.review(studio, project, 1, action="confirm")
    studio.step(project)
    studio.update(project, "failed", "上次请求的验证环境不可用")
    studio.submit(project, message, "继续交付当前应用")
    studio.step(message)
    assert studio.projects()[0]["plan_state"] == "starting"
    assert first_plan.read(studio, project)["state"] == "failed"
    with studio.db.connect() as db:
        goal = db.execute("SELECT goal FROM messages WHERE id=?", (message,)).fetchone()[0]
    assert "linux-x64" in goal


def test_new_desktop_uses_application_prompts_and_its_own_bridge(tmp_path, monkeypatch):
    import yaml
    monkeypatch.setattr(projects, "project_inputs", lambda _: {})
    bot = "inventory-tool"
    projects.new_project(ROOT, tmp_path, bot, "库存工具")
    app = tmp_path / "initial" / "bots" / bot
    assert "库存工具" in (app / "prompts/identity.md").read_text()
    assert "书名" not in (app / "prompts/response-style.md").read_text()
    server, = yaml.safe_load((app / "mcp/servers.yaml").read_text())["servers"]
    assert server["args"][-2] == bot


def test_web_archive_only_contains_owned_verified_candidate_files(tmp_path):
    source = tmp_path / "source"
    bot = "app-a"
    (source / "integrations" / bot).mkdir(parents=True)
    (source / "integrations" / bot / "index.html").write_text("<main>ready</main>")
    (source / "README.md").write_text("framework")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    from chatcopilot.core.source_snapshot import source_manifest, manifest_digest

    manifest = source_manifest(source)
    accepted = SimpleNamespace(
        candidate=source,
        candidate_manifest=manifest,
        receipt={"candidate_digest": manifest_digest(manifest)},
    )
    out = tmp_path / "build"
    project_archive.pack(accepted, out, bot, "web")
    with ZipFile(out / "application.zip") as archive:
        assert archive.namelist() == ["index.html"]
        assert archive.read("index.html") == b"<main>ready</main>"
    assert (
        hashlib.sha256((out / "application.zip").read_bytes()).hexdigest()
        in (out / "build-receipt.json").read_text()
    )
    full = tmp_path / "project-build"
    project_archive.pack(accepted, full, bot, "source")
    with ZipFile(full / "application.zip") as archive:
        assert set(archive.namelist()) == {"README.md", "integrations/app-a/index.html"}
