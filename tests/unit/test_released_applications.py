"""Attach an accepted service without acquiring its source, worker or data volume."""
import hashlib
import io
import json
import tarfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.backend.routes.application_records import router
from console.control import application_installers, released_applications as releases
from console.control.applications import Applications
from studio.installer import FILES, LINUX, WINDOWS


def test_accepted_release_is_local_app_bound_and_repeatable(tmp_path, monkeypatch):
    project, build, task = uuid4().hex, uuid4().hex, "repair-" + uuid4().hex
    ident, bot = "studio:" + project, "app-" + project
    origin = {"kind": "candidate", "task": task, "revision": "b" * 64}
    image = "sha256:" + "c" * 64
    entry = {"service": "web", "port": 8080}
    data = {"application": bot, "entry": entry, "origin": origin, "images": {"test-app": image}}
    root = Path(__file__).resolve().parents[2]
    source, target = (Applications(root, tmp_path / name, discover=lambda: []) for name in ("issuer", "receiver"))
    studio = source.studio()
    studio.create(project)
    with studio.db.connect(write=True) as db:
        db.execute("UPDATE projects SET name=?,bot=?,version='initial' WHERE id=?", ("报修台", bot, project))
        db.execute("INSERT INTO first_plans(project,requested_name,icon,target,approved) VALUES (?,?,?,'source',1)",
                   (project, "报修台", "tools"))
        db.execute("INSERT INTO messages(id,project,text,state,task,version,build,updated) VALUES (?,?,?,'ready',?,'initial',?,1)",
                   (build, project, "fixture", task, build))
    folder = studio.directory(project) / "builds" / build
    folder.mkdir(parents=True)
    (folder / FILES[WINDOWS]).write_bytes(b"windows fixture")
    with (folder / FILES[LINUX]).open("wb") as stream:
        stream.write(b"#!/bin/sh\n__APPLICATION_PAYLOAD__\n")
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            for name, raw in {"compose.yaml": b"services: {web: {image: test-app}}", "install.json": json.dumps(data).encode()}.items():
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                tar.addfile(member, io.BytesIO(raw))
    files = {p: {"sha256": hashlib.sha256((folder / f).read_bytes()).hexdigest(), "size": (folder / f).stat().st_size}
             for p, f in FILES.items()}
    receipt = {"application": bot, "origin": origin, "accepted_source_digest": origin["revision"], "installers": files,
               "docker": True, "components": ["test-app"], "requirements": []}
    (folder / "build-receipt.json").write_text(json.dumps(receipt))
    accepted = {"candidate_digest": origin["revision"], "verification_digest": "d" * 64}
    host = SimpleNamespace(lifecycle=SimpleNamespace(operation=lambda _: nullcontext()))
    monkeypatch.setattr(application_installers.project_repair, "task_host", lambda *a: host)
    monkeypatch.setattr(application_installers, "release_source", lambda *_: SimpleNamespace(task_id=task, bot_id=bot, receipt=accepted))
    assert application_installers.attach_release(source, target, project, build) == application_installers.attach_release(source, target, project, build) == ident
    assert [r["id"] for r in target.catalog()] == [ident]
    row = target.require(ident)
    view = application_installers.catalog(target, row)
    assert view["platforms"] == [WINDOWS, LINUX] and all(r["acceptance"] == accepted for r in view["builds"])
    # Removing the issuing copy cannot affect the imported files. A corrupt re-import cannot replace them.
    (folder / FILES[WINDOWS]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256"):
        application_installers.attach_release(source, target, project, build)
    app = FastAPI()
    app.include_router(router)
    app.state.applications = target
    url = "/api/applications/" + ident
    current = {"image": image, "port": "32778"}
    def docker(*args):
        if args[0] == "ps":
            assert "label=com.docker.compose.project=as-" + bot in args and "label=com.docker.compose.service=web" in args
            return "cid"
        return "true " + current["image"] + " " + json.dumps({"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": current["port"]}]})
    monkeypatch.setattr(releases, "_docker", docker)
    with TestClient(app, client=("127.0.0.1", 1), base_url="http://localhost") as http:
        download = url + "/installers/project-" + build + "/" + WINDOWS
        assert http.get(download).content == b"windows fixture"
        assert http.get(download.replace(build, uuid4().hex)).status_code == 404
        assert http.get(download.replace(project, uuid4().hex)).status_code == 404
        for port in ("32778", "32801"):
            current["port"] = port
            assert http.get(url + "/open", follow_redirects=False).headers["location"] == "http://127.0.0.1:" + port + "/"
        current["image"] = "sha256:" + "e" * 64
        assert http.get(url + "/open", follow_redirects=False).status_code == 409
        stored, _ = releases.artifact(target, row, "project-" + build, WINDOWS)
        stored.write_bytes(b"tampered")
        assert http.get(download).status_code == 400
