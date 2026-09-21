"""Durable input requests and projections of the existing approval authority."""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from uuid import uuid4

import jsonschema

from chatcopilot.contracts.authorization import ApprovalRequest
from chatcopilot.contracts.interactions import ActorResponder, InteractionSnapshot
from chatcopilot.contracts.identity import Role
from chatcopilot.contracts.model_runtime import digest
from chatcopilot.authorization.approvals import hash_approval_challenge
from chatcopilot.core.observability_redaction import (
    redact_observability_payload,
    collect_observability_secrets,
)


class GatewayInteractionService:
    def __init__(self, state, approvals, *, generation, notify=None, clock=time.time):
        self.state, self.approvals = state, approvals
        self.generation, self.notify, self.clock = generation, notify, clock
        self.wake = threading.Event()
        with self.state._write_connection() as connection:
            self.state._assert_generation(connection, generation)
            connection.execute(
                "UPDATE input_requests SET state='cancelled' WHERE state='pending' AND generation!=?",
                (generation,),
            )
            connection.execute(
                "UPDATE approvals SET state='cancelled',challenge='',updated_at=? WHERE state='pending' AND generation!=?",
                (clock(), generation),
            )

    def _row(self, identity):
        with self.state._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM input_requests WHERE interaction_id=?", (identity,)
            ).fetchone()
            if row is None:
                raise ValueError("interaction_not_found")
            columns = [item[1] for item in connection.execute("PRAGMA table_info(input_requests)")]
            return dict(zip(columns, row))

    def _state(self, row):
        if row["kind"] != "approval":
            return row["state"]
        with self.state._read_connection() as connection:
            value = connection.execute(
                "SELECT state,accepted FROM approvals WHERE approval_id=?", (row["approval_id"],)
            ).fetchone()
        if value[0] == "resolved":
            return "approved" if value[1] else "denied"
        return value[0]

    def get(self, identity, responder):
        row = self._row(identity)
        if isinstance(responder, ActorResponder) and responder.actor_ref != row["actor_ref"]:
            raise ValueError("interaction_not_found")
        snapshot = InteractionSnapshot(
            row["interaction_id"],
            row["kind"],
            row["session_id"],
            row["run_id"],
            row["actor_ref"],
            json.loads(row["payload"]),
            row["payload_digest"],
            row["policy_revision"],
            row["created_at"],
            row["expires_at"],
            self._state(row),
        ).to_payload()
        snapshot["responder"] = json.loads(row["responder"]) if row["responder"] else None
        return snapshot

    def list(self, responder, session_id=None):
        with self.state._read_connection() as connection:
            rows = connection.execute(
                "SELECT interaction_id FROM input_requests WHERE (? IS NULL OR session_id=?) "
                "AND (? IS NULL OR actor_ref=?) ORDER BY created_at DESC LIMIT 100",
                (
                    session_id,
                    session_id,
                    responder.actor_ref if isinstance(responder, ActorResponder) else None,
                    responder.actor_ref if isinstance(responder, ActorResponder) else None,
                ),
            ).fetchall()
        return [self.get(row[0], responder) for row in rows]

    def resolve(self, identity, resolution, responder):
        snapshot = self.get(identity, responder)
        row = self._row(identity)
        if (
            snapshot["state"] != "pending"
            or row["expires_at"] <= self.clock()
            or row["generation"] != self.generation
        ):
            raise ValueError("interaction_not_pending")
        if not isinstance(resolution, dict):
            raise ValueError("interaction response must be an object")
        payload = snapshot["payload"]
        if row["kind"] == "approval":
            if set(resolution) != {"decision"} or resolution["decision"] not in {"approve", "deny"}:
                raise ValueError("invalid approval decision")
            with self.state._read_connection() as connection:
                approval = connection.execute(
                    "SELECT challenge,conversation_ref FROM approvals WHERE approval_id=?",
                    (row["approval_id"],),
                ).fetchone()
            receipt = self.approvals.resolve_bound(
                approval_id=row["approval_id"],
                actor_ref=row["actor_ref"],
                conversation_ref=approval[1],
                decision=resolution["decision"],
                challenge=approval[0],
                now=self.clock(),
                responder=responder.to_payload(),
            )
            if not receipt.resolved:
                raise ValueError("interaction_not_pending")
            self.wake.set()
            return {"interactionId": identity, "resolved": True, "executionConfirmed": False}
        elif row["kind"] == "user_input":
            answers = resolution.get("answers")
            questions = payload.get("questions", [])
            if (
                set(resolution) != {"answers"}
                or not isinstance(answers, dict)
                or set(answers) != {q["id"] for q in questions}
            ):
                raise ValueError("answers must match the requested questions")
            for value in answers.values():
                if (
                    not isinstance(value, dict)
                    or set(value) != {"answers"}
                    or not isinstance(value["answers"], list)
                    or not all(isinstance(v, str) for v in value["answers"])
                ):
                    raise ValueError("invalid question answer")
            for question in questions:
                choices = {option["label"] for option in question.get("options") or []}
                if (
                    choices
                    and not question.get("isOther", False)
                    and any(value not in choices for value in answers[question["id"]]["answers"])
                ):
                    raise ValueError("answer must use a declared option")
        else:
            if resolution.get("action") not in {"accept", "decline", "cancel"} or set(
                resolution
            ) - {"action", "content"}:
                raise ValueError("invalid elicitation response")
            if resolution["action"] == "accept" and payload.get("requestedSchema"):
                jsonschema.validate(resolution.get("content"), payload["requestedSchema"])
        with self.state._write_connection() as connection:
            self.state._assert_generation(connection, self.generation)
            changed = connection.execute(
                "UPDATE input_requests SET decision=?,responder=?,state='answered' "
                "WHERE interaction_id=? AND state='pending' AND decision IS NULL AND generation=? AND expires_at>?",
                (
                    json.dumps(resolution),
                    json.dumps(responder.to_payload()),
                    identity,
                    self.generation,
                    self.clock(),
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("interaction_already_resolved")
        self.wake.set()
        return {"interactionId": identity, "resolved": True, "executionConfirmed": False}

    def handler(self, principal, session_id, scope, notify=None):
        def request(method, params, task, cancellation):
            if method == "item/permissions/requestApproval":
                # Scope is a hard ceiling; approval must never widen it.
                return {"permissions": {}, "scope": "turn"}
            approvals = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
            kind = (
                "approval"
                if method in approvals
                else "user_input"
                if method == "item/tool/requestUserInput"
                else "mcp_elicitation"
            )
            if method not in approvals | {
                "item/tool/requestUserInput",
                "mcpServer/elicitation/request",
            }:
                raise ValueError("unsupported_interaction")
            if kind == "approval" and principal.role is not Role.OWNER:
                return {"decision": "decline"}
            if kind == "approval" and params.get("additionalPermissions"):
                return {"decision": "decline"}
            grant_root = params.get("grantRoot")
            if kind == "approval" and grant_root is not None:
                if (
                    not isinstance(grant_root, str)
                    or not Path(grant_root).is_absolute()
                    or not scope.native_write
                    or not scope.permits(Path(grant_root), write=True)
                ):
                    return {"decision": "decline"}
            identity, now = "interaction_" + uuid4().hex, self.clock()
            run_id = task.execution.execution_id
            run = self.state.get_run(run_id)
            if run is None or run.session_id != session_id or run.state != "running":
                raise ValueError("interaction_run_inactive")
            payload = {
                key: value
                for key, value in params.items()
                if key not in {"threadId", "turnId", "itemId"}
            }
            if kind == "user_input" and any(
                q.get("isSecret") for q in payload.get("questions", [])
            ):
                raise ValueError(
                    "secret inputs require a private credential configuration, not a chat interaction"
                )
            if kind == "mcp_elicitation":
                _local_schema_only(payload.get("requestedSchema", {}))
            if kind == "approval":
                payload = redact_observability_payload(
                    payload, secrets=collect_observability_secrets()
                ).value
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > 65536:
                raise ValueError("interaction_payload_too_large")
            approval_id = None
            if kind == "approval":
                approval_id, challenge = identity, secrets.token_urlsafe(24)
                issued = self.approvals.issue(
                    ApprovalRequest(
                        identity,
                        session_id,
                        method,
                        "runtime_operation",
                        "sha256:" + digest(payload),
                        principal.actor_ref,
                        principal.conversation.chat_id,
                        scope.policy_version,
                        hash_approval_challenge(challenge),
                        now + 300,
                        run_id,
                    ),
                    challenge=challenge,
                    now=now,
                )
                if not issued:
                    raise ValueError("approval_issue_failed")
            with self.state._write_connection() as connection:
                self.state._assert_generation(connection, self.generation)
                connection.execute(
                    "INSERT INTO input_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        kind,
                        session_id,
                        run_id,
                        principal.actor_ref,
                        encoded,
                        digest(payload),
                        scope.policy_version,
                        now,
                        now + 300,
                        None if approval_id else "pending",
                        None,
                        None,
                        approval_id,
                        self.generation,
                    ),
                )
            try:
                notifier = notify or self.notify
                if notifier:
                    notifier(
                        principal,
                        session_id,
                        run_id,
                        self.get(
                            identity, ActorResponder(principal.actor_ref, principal.evidence_digest)
                        ),
                    )
                while self.clock() < now + 300:
                    if cancellation:
                        cancellation.raise_if_cancelled()
                    row = self._row(identity)
                    if row["decision"]:
                        resolution = json.loads(row["decision"])
                        if kind == "approval":
                            return {
                                "decision": "accept"
                                if resolution["decision"] == "approve"
                                else "decline"
                            }
                        return resolution
                    self.wake.wait(0.1)
                    self.wake.clear()
                return (
                    {"decision": "decline"}
                    if kind == "approval"
                    else {"answers": {}}
                    if kind == "user_input"
                    else {"action": "cancel", "content": None}
                )
            finally:
                self.cancel(identity, expired=self.clock() >= now + 300)

        return request

    def cancel(self, identity, *, expired=False):
        with self.state._write_connection() as connection:
            self.state._assert_generation(connection, self.generation)
            connection.execute(
                "UPDATE input_requests SET state=? WHERE interaction_id=? AND state='pending'",
                ("expired" if expired else "cancelled", identity),
            )
            connection.execute(
                "UPDATE approvals SET state=?,challenge='',updated_at=? WHERE approval_id=? AND state='pending'",
                ("expired" if expired else "cancelled", self.clock(), identity),
            )

    def respond_text(self, text, principal, session_id):
        pieces = text.strip().split(maxsplit=2)
        if (
            len(pieces) < 2
            or pieces[0] not in {"答复", "批准", "拒绝"}
            or not pieces[1].startswith("interaction_")
        ):
            return False
        responder = ActorResponder(principal.actor_ref, principal.evidence_digest)
        snapshot = self.get(pieces[1], responder)
        if snapshot["sessionId"] != session_id:
            raise ValueError("interaction_not_found")
        if pieces[0] in {"批准", "拒绝"}:
            if principal.role is not Role.OWNER:
                raise ValueError("approval_owner_required")
            resolution = {"decision": "approve" if pieces[0] == "批准" else "deny"}
        else:
            body = pieces[2] if len(pieces) == 3 else ""
            questions = snapshot["payload"].get("questions", [])
            if snapshot["kind"] == "user_input" and len(questions) == 1:
                resolution = {"answers": {questions[0]["id"]: {"answers": [body]}}}
            elif snapshot["kind"] == "user_input" and not body.lstrip().startswith("{"):
                pairs = [piece.strip().split("=", 1) for piece in body.split(";")]
                if any(len(pair) != 2 for pair in pairs) or len({pair[0] for pair in pairs}) != len(
                    pairs
                ):
                    raise ValueError("use question_id=answer; question_id=answer")
                resolution = {"answers": {key: {"answers": [value]} for key, value in pairs}}
            else:
                resolution = json.loads(body)
        self.resolve(pieces[1], resolution, responder)
        return True


def _local_schema_only(value):
    if isinstance(value, dict):
        if "$ref" in value and not str(value["$ref"]).startswith("#"):
            raise ValueError("remote schema references are not supported")
        for item in value.values():
            _local_schema_only(item)
    elif isinstance(value, list):
        for item in value:
            _local_schema_only(item)


def format_interaction(snapshot):
    identity, payload = snapshot["interactionId"], snapshot["payload"]
    lines = ["待处理请求 " + identity]
    if snapshot["kind"] == "user_input":
        questions = payload.get("questions", [])
        for question in questions:
            lines.append(question["id"] + ": " + question["question"])
            for option in question.get("options") or []:
                lines.append("- " + option["label"] + ": " + option.get("description", ""))
        lines.append(
            "答复 " + identity + (" <内容>" if len(questions) == 1 else " q1=内容; q2=内容")
        )
    elif snapshot["kind"] == "approval":
        lines.append(str(payload.get("command") or payload.get("reason") or "原生操作审批")[:1600])
        lines.append("批准 " + identity + " 或 拒绝 " + identity)
    else:
        lines.append(str(payload.get("message", "服务请求输入")))
        lines.append(
            "请在 Console 完成表单或 URL 确认；QQ 也可用 答复 " + identity + " <JSON 答复>。"
        )
    lines.append("请求有有效期；答复仅记录决定，不代表工具执行成功。")
    return "\n".join(lines)
