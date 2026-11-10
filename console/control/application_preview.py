"""Application-bound preview; UI cannot choose a credential, URL or channel session."""
from __future__ import annotations

import asyncio
import json
import time
from uuid import UUID

from chatcopilot.core.private_sqlite import private_file
from chatcopilot.protocols.gateway_client import GatewayClientConfig, GatewayWebSocketClient, GatewayRemoteError
from chatcopilot.contracts.gateway_rpc import (
    SessionsGetParams, SessionsCreateParams, RunsGetParams, ChatSendParams, ChatAbortParams, TextRpcSegment,
)
from chatcopilot.contracts.gateway_rpc import client_run_id

CLIENT = "console-preview"
TERMINAL = {"completed", "aborted", "failed"}


class Preview:
    def __init__(self, applications):
        self.apps = applications

    def row(self, ident):
        row = self.apps.require(ident)
        if row["source"] != "studio" or row["type"] != "qq_official":
            raise PermissionError("此应用尚未接入安全试聊")
        return row

    def settings(self, row):
        manager = self.apps.manager()
        if manager._active(row["project_id"]) != "active":
            return None, "请先启动应用；试聊不会自动启动或重启 QQ 应用。"
        path = manager._root(row["project_id"]) / "runtime.env"
        if not path.is_file():
            return None, "应用缺少安全试聊身份，请手动停止后重新启动。"
        private_file(path)
        values = {}
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            if key in {"CHATCOPILOT_GATEWAY_PORT", "AGENTSTRATA_PREVIEW_TOKEN"}:
                values[key] = json.loads(value)
        if not values.get("AGENTSTRATA_PREVIEW_TOKEN"):
            return None, "当前进程尚无安全试聊身份，请手动停止后重新启动。"
        port = int(values.get("CHATCOPILOT_GATEWAY_PORT", 0))
        if not 0 < port < 65536:
            raise ValueError("试聊连接配置无效")
        return GatewayClientConfig(url=f"ws://127.0.0.1:{port}", token=values["AGENTSTRATA_PREVIEW_TOKEN"],
                                   client_id=CLIENT, client_mode="interactive"), "试聊只在工作台回复，不发送到 QQ。"

    def database(self):
        with self.apps.studio().db.connect(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS console_preview (id TEXT PRIMARY KEY, project TEXT NOT NULL, "
                       "text TEXT NOT NULL, state TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', "
                       "error_code TEXT, updated REAL NOT NULL)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_preview_run ON console_preview(project) "
                       "WHERE state NOT IN ('completed','aborted','failed')")

    def items(self, project):
        with self.apps.studio().db.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,text,state,reply,error_code,updated FROM console_preview "
                                               "WHERE project=? ORDER BY rowid", (project,))]

    def save(self, project, key, state, reply="", error=None):
        with self.apps.studio().db.connect(write=True) as db:
            db.execute("UPDATE console_preview SET state=?,reply=?,error_code=?,updated=? WHERE project=? AND id=?",
                       (state, reply, error, time.time(), project, key))

    @staticmethod
    def session(project):
        return "console-preview-" + project

    async def exchange(self, row, config, item, mode):
        project, key = row["project_id"], item["id"]
        session = self.session(project)
        run = client_run_id(client_id=CLIENT, session_id=session, idempotency_key=key)
        client = GatewayWebSocketClient(config)
        try:
            await client.connect()
            # Query first, even on a duplicate POST. A disconnect never means a new model run.
            try:
                result = await client.request("runs.get", RunsGetParams(session, run))
            except GatewayRemoteError as exc:
                if exc.code not in {"run_not_found", "session_not_found"}:
                    raise
                result = None
            if result is not None:
                current = result.run
                reply = "\n".join(s.text for s in current.segments if isinstance(s, TextRpcSegment))
                self.save(project, key, current.state, reply,
                          "preview_run_failed" if current.error_code else None)
                if mode == "abort" and current.state not in TERMINAL:
                    await client.request("chat.abort", ChatAbortParams(session, run), idempotency_key="abort-" + key)
                    self.save(project, key, "abort_requested", reply)
                return
            if mode not in {"send", "retry"}:
                self.save(project, key, "unknown", error="preview_outcome_unknown")
                return
            try:
                await client.request("sessions.get", SessionsGetParams(session))
            except GatewayRemoteError as exc:
                if exc.code != "session_not_found":
                    raise
                await client.request("sessions.create", SessionsCreateParams(session_id=session),
                                     idempotency_key="session-" + project)
            result = await client.request("chat.send", ChatSendParams(session, (TextRpcSegment(item["text"]),), key),
                                          idempotency_key=key)
            if result.run_id != run or result.session_id != session:
                raise ValueError("试聊运行身份不匹配")
            self.save(project, key, "accepted")
        finally:
            await client.close()

    def operate(self, ident, mode="read", key=None, text=None):
        row = self.row(ident)
        self.database()
        project = row["project_id"]
        with self.apps._operation_lock(row):
            config, reason = self.settings(row)
            if key is not None:
                key = UUID(str(key)).hex
            if mode == "send":
                text = (text or "").strip()
                if not text or len(text) > 16000:
                    raise ValueError("请输入有效的试聊内容")
                if config is None:
                    raise ValueError(reason)
                with self.apps.studio().db.connect(write=True) as db:
                    existing = db.execute("SELECT project,text FROM console_preview WHERE id=?", (key,)).fetchone()
                    if existing and (existing["project"], existing["text"]) != (project, text):
                        raise ValueError("同一请求不能更换应用或内容")
                    if not existing:
                        db.execute("INSERT INTO console_preview(id,project,text,state,updated) VALUES (?,?,?,'queued',?)",
                                   (key, project, text, time.time()))
            items = self.items(project)
            if key and not any(i["id"] == key for i in items):
                raise KeyError("试聊记录不存在")
            for item in items:
                if item["state"] in TERMINAL or (key and item["id"] != key):
                    continue
                if config is None:
                    break
                try:
                    asyncio.run(asyncio.wait_for(self.exchange(row, config, item, mode), timeout=15))
                except Exception as exc:
                    latest = next(value for value in self.items(project) if value["id"] == item["id"])
                    recovery = isinstance(exc, GatewayRemoteError) and exc.code == "run_recovery_required"
                    self.save(project, item["id"], "recovery_required" if recovery else "unknown",
                              latest["reply"], "preview_recovery_required" if recovery else "preview_connection_unavailable")
            return {"available": config is not None, "reason": reason, "messages": self.items(project)}
