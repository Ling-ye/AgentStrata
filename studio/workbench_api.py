"""Authenticated, deterministic standard application actions; no generated shell."""
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="QQ 官方机器人", min_length=1, max_length=100)
    instructions: str = Field(default="", max_length=8000)
    app_id: str = Field(default="", max_length=64)
    app_secret: str = Field(default="", max_length=512)


def application_routes(studio):
    from studio.applications import ApplicationManager
    manager = ApplicationManager(studio.state_dir, studio.framework)
    router = APIRouter()

    def standard(project):
        with studio.db.connect() as db:
            row = db.execute("SELECT bot FROM projects WHERE id=?", (project.hex,)).fetchone()
        if not row:
            raise KeyError("应用不存在")
        if not row["bot"].startswith("qq-official-"):
            raise HTTPException(409, "此操作仅适用于 QQ 官方机器人应用")
        return project.hex

    @router.get("/api/projects/{project}/application")
    def status(project: UUID):
        return manager.get(standard(project))

    @router.post("/api/projects/{project}/application/configure")
    def configure(project: UUID, body: Connection):
        result = manager.configure(standard(project), body.model_dump())
        with studio.db.connect(write=True) as db:
            db.execute("UPDATE projects SET name=? WHERE id=?", (body.name, project.hex))
        return result

    @router.post("/api/projects/{project}/application/proposals/{message}/apply")
    def apply(project: UUID, message: UUID):
        key = standard(project)
        with studio.db.connect() as db:
            row = db.execute("SELECT goal,state FROM messages WHERE project=? AND id=?", (key, message.hex)).fetchone()
            latest = db.execute("SELECT id FROM messages WHERE project=? AND state='configure' ORDER BY rowid DESC LIMIT 1", (key,)).fetchone()
        if not row or row["state"] != "configure" or not latest or latest["id"] != message.hex:
            raise HTTPException(409, "这份配置提案已失效，请使用最新提案")
        proposal = json.loads(row["goal"])
        result = manager.configure(key, proposal)
        with studio.db.connect(write=True) as db:
            db.execute("UPDATE projects SET name=? WHERE id=?", (proposal["name"], key))
        return result

    @router.post("/api/projects/{project}/application/check")
    def check(project: UUID):
        return manager.check(standard(project))

    @router.post("/api/projects/{project}/application/start")
    def start(project: UUID):
        return manager.start(standard(project))

    @router.post("/api/projects/{project}/application/stop")
    def stop(project: UUID):
        return manager.stop(standard(project))

    return router
