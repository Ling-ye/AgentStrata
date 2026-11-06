"""Project isolation, real UI/MCP shared storage and the accepted-build boundary."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from integrations.desktop_app.bridge import application_actions
from studio import runtime
from studio.server import application
from studio.workspace import Studio

ROOT = Path(__file__).resolve().parents[2]


def test_mcp_and_page_use_same_book_list(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    data = tmp_path / "books"
    ui = application_actions(ROOT, "desktop-reader", data)
    ui["add_book"]("小王子")
    async def agent():
        server = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / "integrations/desktop_app/bridge.py"), "mcp", str(ROOT), "desktop-reader", str(data)],
            env={"PYTHONPATH":str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE":"1"})
        async with stdio_client(server) as (reader, writer), ClientSession(reader, writer) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == {"list_books", "add_book", "set_read"}
            result = await client.call_tool("set_read", {"title":"小王子", "finished":True})
            assert not result.isError
    asyncio.run(agent())
    assert ui["list_books"]() == [{"title":"小王子", "finished":True}]
    assert application_actions(ROOT, "desktop-reader", tmp_path / "other")["list_books"]() == []


def test_local_api_idempotency_and_restart(tmp_path):
    ids = [uuid4() for _ in range(2)]
    rid = uuid4()
    def run():
        return TestClient(application(tmp_path, ROOT, "key", run_worker=False), base_url="http://localhost")
    with run() as client:
        assert client.get("/api/projects").status_code == 401
        assert client.post("/api/login", json={"token":"key"}, headers={"origin":"https://foreign.example"}).status_code == 403
        assert client.post("/api/login", json={"token":"key"}).status_code == 200
        for p in ids:
            assert client.post("/api/projects", json={"id":p.hex}).status_code == 200
        url = f"/api/projects/{ids[0]}/messages"
        msg = {"id":rid.hex,"text":"做一个读书助手"}
        assert client.post(url, json=msg).status_code == 200
        assert client.post(url, json=msg).status_code == 200
        assert client.post(url, json={**msg, "id":uuid4().hex}).status_code == 409
        assert client.post(f"/api/projects/{ids[1]}/messages", json=msg).status_code == 400
    with run() as client:
        client.post("/api/login", json={"token":"key"})
        assert [m["id"] for m in client.get(url).json()] == [rid.hex]
        assert client.get(f"/api/projects/{ids[1]}/messages").json() == []
        assert client.get(f"/api/projects/{ids[1]}/builds/{rid}").status_code == 404


@pytest.mark.parametrize("reply,action", [('{"action":"develop","reply":"准备改动","goal":"添加书单"}',"develop"),
                                        ("你希望怎样整理书单？","discuss")])
def test_guide_keeps_gateway_source_notice(tmp_path, monkeypatch, reply, action):
    from integrations.desktop_app import bridge
    from chatcopilot.project import source_notice
    async def answer(*_args):
        return reply + "\n\n" + source_notice()
    monkeypatch.setattr(bridge, "chat", answer)
    (tmp_path/"guide.json").write_text(json.dumps({"url":"local", "token":"fixture"}))
    decision = asyncio.run(runtime.discuss(tmp_path, "project", "request", "原文"))
    assert decision["action"] == action
    assert source_notice() in decision["reply"]



def test_guide_can_recover_a_plain_reply_without_changing_the_request(tmp_path, monkeypatch):
    from integrations.desktop_app import bridge
    from chatcopilot.project import source_notice

    calls = []
    async def answer(_url, _token, _client, _session, key, prompt):
        calls.append((key, prompt))
        if len(calls) == 1:
            return "我可以帮你开发。" + "\n\n" + source_notice()
        return '{"action":"develop","reply":"开始修改当前应用"}'
    monkeypatch.setattr(bridge, "chat", answer)
    (tmp_path / "guide.json").write_text(json.dumps({"url": "local", "token": "fixture"}))
    decision = asyncio.run(runtime.discuss(tmp_path, "project", "msg", "加一个按钮"))
    assert decision == {"action": "develop", "reply": "开始修改当前应用\n\n" + source_notice()}
    assert [key for key, _ in calls] == ["msg", "msg-format"]
    assert calls[1][1].endswith("加一个按钮")


def test_new_source_scope_and_resume(tmp_path, monkeypatch):
    import shutil
    from studio.projects import source_tree
    from chatcopilot.core.source_snapshot import source_manifest
    # The host may run these regression tests from a frozen tree without Git metadata.
    fixture = tmp_path / "fixture"
    for name in ("bots/desktop-reader", "studio/seed"):
        shutil.copytree(ROOT / name, fixture / name)
    shutil.copyfile(ROOT / "studio/seed_tests.py", fixture / "studio/seed_tests.py")
    import subprocess
    subprocess.run(["git", "init", "-q", str(fixture)], check=True)
    framework = source_tree(tmp_path / "public", "framework", fixture, source_manifest(fixture))
    studio = Studio(tmp_path / "state", framework)
    project, message = uuid4().hex, uuid4().hex
    studio.create(project)
    studio.submit(project, message, "做一个新应用")
    async def develop(*args):
        return {"action":"develop","goal":"模型额外要求精确匹配原始空格","name":"本机助手","reply":"准备开发"}
    monkeypatch.setattr(runtime, "discuss", develop)
    studio.step(message)
    row = studio.conversation(project)[0]
    assert row["state"] == "starting"
    source = studio.directory(project) / "initial"
    import yaml
    app = source / "bots" / ("app-" + project)
    assert yaml.safe_load((app / "bot.yaml").read_text())["display_name"] == "本机助手"
    assert "本机助手" in (app / "prompts/identity.md").read_text()
    assert not (source / "integrations/desktop-reader").exists()
    assert sorted(p.name for p in (source / "bots").iterdir()) == ["app-" + project]
    assert (source / "tests/unit/test_desktop_application.py").is_file()
    calls = []
    def start(repo, state, bot, goal, req):
        calls.append((repo, bot, goal, req))
        return {"task_id":"controlled-test"}
    monkeypatch.setattr(runtime, "start_task", start)
    Studio(studio.state_dir, framework).step(message)
    assert "AGENTSTRATA_DESKTOP_DATA" in (source / "bots" / ("app-" + project) / "mcp/servers.yaml").read_text()
    assert calls == [(source, "app-" + project, "做一个新应用", message)]
    assert studio.conversation(project)[0]["task"] == "controlled-test"


@pytest.mark.parametrize(("stage", "expected"), [
    ("coding", "developing"), ("verify-2-r3", "verifying"), ("regressions", "verifying"),
    ("repository-verify-2", "verifying"), ("confirm-1", "verifying"),
])
def test_project_card_reflects_recorded_harness_stages(tmp_path, monkeypatch, stage, expected):
    from types import SimpleNamespace

    studio = Studio(tmp_path, ROOT)
    project, message = uuid4().hex, uuid4().hex
    studio.create(project)
    studio.submit(project, message, "改一下窗口")
    studio.update(message, "developing", version="initial", task="controlled-task")
    task = {"status": "running", "stage": stage}
    def controller(_repo, _state, *, task_id):
        assert task_id == "controlled-task"
        return SimpleNamespace(get=lambda _: task)
    monkeypatch.setattr(runtime, "controller", controller)
    studio.step(message)
    assert studio.conversation(project)[0]["state"] == expected


def test_confirmed_discussion_comes_from_the_same_chat(tmp_path, monkeypatch):
    studio = Studio(tmp_path, ROOT)
    project, other, previous, current = (uuid4().hex for _ in range(4))
    studio.create(project)
    studio.create(other)
    with studio.db.connect(write=True) as db:
        db.execute("UPDATE projects SET version='initial' WHERE id=?", (project,))
    studio.submit(project, previous, "怎么表示已读？")
    studio.update(previous, "discuss", "可以在每本书旁边放一个勾选框。")
    studio.submit(other, uuid4().hex, "另一个项目的资料")
    studio.submit(project, current, "按刚才说的做")
    async def confirm(*_):
        return {"action": "develop", "use_previous": True, "reply": "开始开发", "goal": "无依据的新要求"}
    monkeypatch.setattr(runtime, "discuss", confirm)
    studio.step(current)
    with studio.db.connect() as db:
        goal = db.execute("SELECT goal FROM messages WHERE id=?", (current,)).fetchone()[0]
    assert goal.startswith("按刚才说的做")
    assert "可以在每本书旁边放一个勾选框。" in goal
    assert "另一个项目" not in goal and "无依据" not in goal




@pytest.mark.parametrize("bot", ["desktop-reader", "live2d-desktop"])
def test_harness_receives_original_goal_with_application_write_scope(tmp_path, monkeypatch, bot):
    from types import SimpleNamespace
    from chatcopilot.external_tools.codex_cli import auth_cli

    profile = SimpleNamespace(credential_root=str(tmp_path), model="fixture", reasoning_effort="medium", profile_id="dev")
    monkeypatch.setattr(runtime, "worker_profile", lambda *_args, **_opts: (profile, {}))
    monkeypatch.setattr(auth_cli, "status_lanes", lambda *_: [SimpleNamespace(state="ready")])
    captured = []
    def start(request, options, request_id):
        captured.append((request, options, request_id))
        return {"task_id":"controlled"}
    monkeypatch.setattr(runtime, "controller", lambda *_args, **_opts: SimpleNamespace(start_improvement=start))
    prompt = "我说“开工啦”时请先露出开心表情。"
    runtime.start_task(ROOT, tmp_path, bot, prompt, "client-request")
    request, options, request_id = captured[0]
    assert request.goal == prompt and request_id == "client-request"
    assert options.timeout_seconds == 3600 and options.max_attempts == 3
    if bot == "live2d-desktop":
        assert "integrations/open_llm_vtuber/agentstrata_vtuber/agent.py" in request.allowed_paths
        assert "integrations/open_llm_vtuber/agentstrata_vtuber/avatar.py" in request.allowed_paths
    else:
        assert f"integrations/{bot}" in request.allowed_paths


def test_followup_uses_last_accepted_source_and_reports_verified_behavior(tmp_path, monkeypatch):
    from contextlib import nullcontext
    import subprocess
    from types import SimpleNamespace
    from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
    from chatcopilot.harness import activation_runtime
    from studio import delivery

    studio = Studio(tmp_path / "state", ROOT)
    project, first, second = (uuid4().hex for _ in range(3))
    studio.create(project)
    work = studio.directory(project)
    candidate = tmp_path / "controlled-candidate"
    candidate.mkdir()
    subprocess.run(["git", "init", "-q", str(candidate)], check=True)
    (candidate / "reading.txt").write_text("第一版书单")
    manifest = source_manifest(candidate)
    version = manifest_digest(manifest)
    with studio.db.connect(write=True) as db:
        db.execute("UPDATE projects SET version='initial' WHERE id=?", (project,))
    studio.submit(project, first, "做一个书单")
    studio.update(first, "packaging", task="controlled-first", version="initial", goal="做一个书单")
    task = {"status":"fixed", "accepted_candidate":{"candidate_digest":version},
            "acceptance":{"items":[{"required":True, "text":"可以添加书名"}]}}
    host = SimpleNamespace(get=lambda _id: task, lifecycle=SimpleNamespace(operation=lambda _id: nullcontext()))
    release = SimpleNamespace(receipt={"candidate_digest":version}, candidate=candidate,
                              candidate_manifest=manifest, bot_relative="bots/sample/bot.yaml")
    def controller(_repo, _state, *, task_id):
        assert task_id == "controlled-first"
        return host
    monkeypatch.setattr(runtime, "controller", controller)
    monkeypatch.setattr(activation_runtime, "release_source", lambda *_: release)
    def pack(_repo, stage, **_opts):
        stage.mkdir(parents=True)
        file = stage / "application.exe"
        file.write_bytes(b"controlled artifact")
        (stage / "build-receipt.json").write_text(json.dumps(
            {"accepted_source_digest":version, "installer_sha256":delivery.digest(file)}))
    monkeypatch.setattr(delivery, "pack", pack)
    studio.step(first)
    ready = studio.conversation(project)[0]
    assert ready["state"] == "ready" and "可以添加书名" in ready["reply"] and "试用方法" in ready["reply"]
    assert (work / version / "reading.txt").read_text() == "第一版书单"
    studio.submit(project, second, "加一个已读勾选框")
    async def decide(*_):
        return {"action":"develop", "reply":"开始"}
    monkeypatch.setattr(runtime, "discuss", decide)
    calls = []
    def start(repo, _state, _bot, goal, request, *, predecessor_task_id=None):
        calls.append((manifest_digest(source_manifest(repo)), goal, request, predecessor_task_id))
        return {"task_id":"controlled-second"}
    monkeypatch.setattr(runtime, "start_task", start)
    studio.step(second)
    studio.step(second)
    studio.submit(project, second, "加一个已读勾选框")
    assert calls == [(version, "加一个已读勾选框", second, "controlled-first")]
    assert [m["task"] for m in studio.conversation(project)] == ["controlled-first", "controlled-second"]


def test_packaging_projects_only_the_target_application():
    from studio.delivery import native_inputs
    names = ["bots/books/agent.yaml", "integrations/books/actions.py", "src/chatcopilot/application_cli.py",
             "integrations/desktop_app/bridge.py", "bots/other/bot.yaml", "integrations/other/actions.py",
             "integrations/open_llm_vtuber/README.md", "tests/unit/test_sample.py",
             "integrations/desktop_app/select-live2d.ps1", "integrations/desktop_app/observe.ps1"]
    licenses = {"LICENSE", "COPYRIGHT", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.txt"}
    rows = {name: {"sha256":str(i)} for i, name in enumerate(names)}
    assert native_inputs(rows | dict.fromkeys(licenses), "books").keys() == set(names[:4]) | licenses


def test_accepted_receipt_binds_distinct_runtime_projection(tmp_path):
    import shutil
    import subprocess
    from chatcopilot.contracts.deployment import ReleaseSource
    from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
    from chatcopilot.deployment.application_bundle import read_bundle
    from studio.delivery import build_payload

    root = tmp_path / "source"
    shutil.copytree(ROOT / "bots/desktop-reader", root / "bots/desktop-reader")
    for p in ("pyproject.toml", "uv.lock", "src/chatcopilot/__main__.py", "src/chatcopilot/application_cli.py",
              "src/chatcopilot/deployment/windows_runtime.py", "deploy/wsl/install_wsl_env.sh",
              "deploy/wsl/update_instance.sh", "integrations/live2d_desktop/scripts/bootstrap-windows.ps1",
              "integrations/live2d_desktop/windows-runtime.lock.json", "integrations/live2d_desktop/windows-host-requirements.lock",
              "integrations/other-project/private-business-logic.py"):
        path = root / p
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("controlled fixture\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                    "commit", "--allow-empty", "-qm", "fixture"], check=True)
    manifest = source_manifest(root)
    approved = {"candidate_digest":manifest_digest(manifest), "goal_digest":"goal",
                "verification_digest":"verify","review_binding":"review"}
    release = ReleaseSource("test-task", "desktop-reader", root, manifest, root, manifest, approved,
                            "bots/desktop-reader/bot.yaml", {}, "controlled-model")
    receipt = build_payload(root, tmp_path / "export", bot_relative=release.bot_relative, release=release)
    windows = read_bundle(tmp_path / "export/runtime-payload")
    assert receipt["accepted_source_digest"] == approved["candidate_digest"]
    assert windows["source_digest"] == receipt["source_digest"] != receipt["accepted_source_digest"]
    assert "integrations/other-project/private-business-logic.py" not in windows["source_manifest"]
    assert "accepted_candidate" not in windows


@pytest.mark.parametrize(("failure", "message"), [("timeout", "超时"), ("process", "完成本机准备")])
def test_live2d_selection_explains_windows_failure(monkeypatch, failure, message):
    import subprocess
    from studio.projects import select_live2d

    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_opts: "selector.ps1")
    def failed(*_args, **_opts):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("powershell", 300)
        raise subprocess.CalledProcessError(1, "powershell")
    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(ValueError, match=message):
        select_live2d()



def test_studio_runs_its_own_worker_for_the_imported_project(tmp_path):
    from chatcopilot.harness.worker_runtime import SystemdWorkerControl

    repo = tmp_path / "external"
    repo.mkdir()
    host = runtime.controller(repo, tmp_path / "state")
    worker = host.lifecycle.workers
    assert isinstance(worker, SystemdWorkerControl)
    assert worker.repository == repo
    frozen, digest = runtime.verification_runtime(tmp_path / "state")
    assert worker.runtime_repository == frozen
    assert host.evaluator.verification_runtime == digest
    assert (frozen / "src/chatcopilot/harness/worker.py").read_bytes() == (ROOT / "src/chatcopilot/harness/worker.py").read_bytes()


def test_new_project_runs_and_preserves_its_application_regressions(tmp_path):
    from chatcopilot.harness.local_verifier import LocalVerifier
    from studio.projects import new_project

    project = new_project(ROOT, tmp_path / "project", "app-contract", "应用接口")
    assert [p.name for p in (project / "bots").iterdir()] == ["app-contract"]
    assert not (project / "studio").exists()
    assert not (project / "tests/unit/test_harness_sources.py").exists()
    assert (project / "src/chatcopilot/agent/runtime.py").is_file()
    verifier = LocalVerifier(tmp_path / "verification")
    task = {"task_id": "fixture", "source": {"kind": "application"}}
    baseline = verifier.regressions(task, project, lambda: None)
    assert baseline["failed_cases"] == []
    assert sum(name.startswith("tests/unit/") for name in baseline["case_ids"]) == 2
    (project / "integrations/app-contract/actions.py").write_text("def actions(data):\n    return {}\n")
    candidate = verifier.regressions(task, project, lambda: None, baseline["case_ids"])
    assert candidate["case_ids"] == baseline["case_ids"]
    assert candidate["failed_cases"] == [
        "tests/unit/test_desktop_application.py::test_desktop_application_exposes_named_operations"]


def test_parallel_application_regressions_compare_every_frozen_case(tmp_path):
    import subprocess
    from chatcopilot.harness.local_verifier import LocalVerifier

    repo = tmp_path / "project"
    tests = repo / "tests/unit"
    tests.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    first = tests / "test_first.py"
    first.write_text("import os\ndef test_one():\n    assert os.environ.get('PYTEST_XDIST_WORKER')\n")
    second = tests / "test_second.py"
    second.write_text("def test_two():\n    assert True\n")
    task = {"task_id": "fixture", "source": {"kind": "application"}}
    verifier = LocalVerifier(tmp_path / "harness")
    baseline = verifier.regressions(task, repo, lambda: None)
    assert len(baseline["case_ids"]) == len(baseline["passed_cases"]) == 2
    second.write_text("def test_two():\n    assert False\n\ndef test_new():\n    assert True\n")
    candidate = verifier.regressions(task, repo, lambda: None, baseline["case_ids"])
    assert candidate["case_ids"] == baseline["case_ids"]
    assert candidate["passed_cases"] == ["tests/unit/test_first.py::test_one"]
    assert candidate["failed_cases"] == ["tests/unit/test_second.py::test_two"]


@pytest.mark.parametrize("corruption", ["before_execution", "after_execution"])
def test_corrupt_execution_copy_is_an_environment_failure(tmp_path, monkeypatch, corruption):
    import subprocess
    from chatcopilot.harness import local_verifier
    from chatcopilot.harness.models import HarnessError

    repo = tmp_path / "project"
    tests = repo / "tests/unit"
    tests.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "value.py").write_text("VALUE = 1\n")
    (tests / "test_value.py").write_text("from value import VALUE\ndef test_value():\n    assert VALUE == 1\n")
    original_copy = local_verifier.copy_sources
    original_remove = local_verifier.shutil.rmtree
    def copy(source, destination, manifest):
        original_copy(source, destination, manifest)
        if corruption == "before_execution":
            (destination / "value.py").write_bytes(b"\0" * 10)
    def remove(path, *args, **kwargs):
        original_remove(path, *args, **kwargs)
        if (corruption == "after_execution" and Path(path).name == "tmp"
                and Path(path).parent.parent == tmp_path / "verification/jobs/fixture/checks"):
            (Path(path).parent / "source/value.py").write_bytes(b"\0" * 10)
    monkeypatch.setattr(local_verifier, "copy_sources", copy)
    monkeypatch.setattr(local_verifier.shutil, "rmtree", remove)
    verifier = local_verifier.LocalVerifier(tmp_path / "verification")
    with pytest.raises(HarnessError, match="验证输入副本") as error:
        verifier.regressions({"task_id": "fixture", "source": {}}, repo, lambda: None)
    assert error.value.code == "verification_environment"
    assert (repo / "value.py").read_text() == "VALUE = 1\n"


def test_trial_ports_are_distinct_and_stable():
    from chatcopilot.windows_setup import trial_ports
    ports = trial_ports({}, "PORT")
    assert ports["port"] != ports["gateway_port"]
    assert ports == trial_ports({"PORT":str(ports["gateway_port"]),
        "AGENTSTRATA_AVATAR_MCP_URL":f"http://127.0.0.1:{ports['port']}/agentstrata/mcp/"}, "PORT")


def test_verification_runtime_is_frozen_and_socket_tracks_host_version(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from studio import runtime
    from studio.projects import socket_path

    platform = tmp_path / "host"
    platform.mkdir()
    subprocess.run(["git", "init", "-q", str(platform)], check=True)
    module = platform / "src/chatcopilot/evals/rules.py"
    module.parent.mkdir(parents=True)
    module.write_text("VALUE = 'first'\n")
    monkeypatch.setattr(runtime, "__file__", str(platform / "studio/runtime.py"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "sockets"))
    runtime.verification_runtime.cache_clear()
    state = tmp_path / "project"
    try:
        frozen, identity = runtime.verification_runtime(state)
        module.write_text("VALUE = 'next host'\n")
        assert runtime.verification_runtime(state) == (frozen, identity)
        assert (frozen / "src/chatcopilot/evals/rules.py").read_text() == "VALUE = 'first'\n"
        runtime.verification_runtime.cache_clear()
        next_root, next_identity = runtime.verification_runtime(state)
        assert next_identity != identity and next_root != frozen
        assert socket_path(state, identity) != socket_path(state, next_identity)
        monkeypatch.setattr(runtime, "HarnessController", lambda *a, **k: SimpleNamespace(
            get=lambda _: {"source": {"verification_runtime": identity}}))
        assert runtime.task_verification_runtime(platform, state, "existing") == (frozen, identity)
        (frozen / "src/chatcopilot/evals/rules.py").write_text("TAMPERED = True\n")
        runtime.task_verification_runtime.cache_clear()
        runtime.task_verification_id.cache_clear()
        with pytest.raises(ValueError, match="验证快照已变化"):
            runtime.task_verification_runtime(platform, state, "existing")
    finally:
        runtime.verification_runtime.cache_clear()
        runtime.task_verification_runtime.cache_clear()


def test_old_task_retry_is_read_only_and_new_request_can_use_same_conversation(tmp_path, monkeypatch):
    project, previous, current = (uuid4() for _ in range(3))
    app = application(tmp_path, ROOT, "fixture-key", run_worker=False)
    studio = app.state.studio
    studio.create(project.hex)
    with studio.db.connect(write=True) as db:
        db.execute("UPDATE projects SET version='initial' WHERE id=?", (project.hex,))
    studio.submit(project.hex, previous.hex, "old goal")
    studio.update(previous.hex, "failed", "原失败证据", task="old-task", version="initial")
    monkeypatch.setattr(runtime, "task_verification_id", lambda *_: None)
    before = studio.conversation(project.hex)[0]
    assert before["retryable"] is False
    with TestClient(app, base_url="http://localhost") as client:
        client.post("/api/login", json={"token":"fixture-key"})
        response = client.post(f"/api/projects/{project}/messages/{previous}/retry", json={})
        assert response.status_code == 409 and "只读" in response.json()["detail"]
        assert studio.conversation(project.hex)[0] == before
        assert client.post(f"/api/projects/{project}/messages", json={"id":current.hex,"text":"new goal"}).status_code == 200
        assert studio.conversation(project.hex)[0] == before
