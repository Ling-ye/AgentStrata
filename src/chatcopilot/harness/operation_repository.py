"""Durable external-operation facts, independent of LangGraph checkpoints."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.models import HarnessError


class OperationRepository:
    """Use the existing task transaction; operations never modify lifecycle status."""

    def __init__(self, store, task_id: str):
        self.store, self.task_id = store, task_id

    def get(self, key: str, binding: str | None = None) -> dict[str, Any] | None:
        value = self.store.control_state(self.task_id).get("operations", {}).get(key)
        if value and binding is not None and value["binding"] != binding:
            raise HarnessError("operation_changed", "外部操作的冻结输入或执行身份发生变化")
        return value

    def start(
        self,
        key: str,
        *,
        kind: str,
        node: str,
        attempt: int,
        binding: str,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        return self._write(
            key,
            binding,
            {
                "kind": kind,
                "node": node,
                "attempt": attempt,
                "state": "started",
                "external_id": external_id,
                "started_at": time.time(),
                "operation_id": hashlib.sha256(
                    json_text(
                        {"task": self.task_id, "node": key, "attempt": attempt, "binding": binding}
                    ).encode()
                ).hexdigest(),
            },
            starting=True,
        )

    def complete(self, key: str, binding: str, result_ref: dict, **facts) -> dict:
        return self._write(
            key,
            binding,
            {**facts, "state": "completed", "result_ref": result_ref, "finished_at": time.time()},
        )

    def fail(self, key: str, binding: str, code: str, message: str, *, unknown=False) -> dict:
        return self._write(
            key,
            binding,
            {
                "state": "unknown" if unknown else "failed",
                "error": {"code": code, "message": message},
                "finished_at": time.time(),
            },
        )

    def _write(self, key: str, binding: str, fields: dict, *, starting=False) -> dict:
        with self.store.database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE task_id=?", (self.task_id,)
            ).fetchone()
            if row is None:
                raise HarnessError("not_found", "修复任务不存在")
            task = json.loads(row[0])
            operations = task.setdefault("operations", {})
            previous = operations.get(key)
            if previous and previous["binding"] != binding:
                raise HarnessError("operation_changed", "外部操作的冻结输入或执行身份发生变化")
            if starting and previous:
                return previous
            if not starting and not previous:
                raise HarnessError("verification_evidence", "外部操作缺少执行前回执")
            if previous and previous["state"] == "completed":
                if (
                    fields.get("state") != "completed"
                    or fields.get("result_ref") != previous["result_ref"]
                ):
                    raise HarnessError("operation_changed", "已完成操作的结果不能被覆盖")
                return previous
            if previous and previous["state"] == "failed" and fields.get("state") != "failed":
                raise HarnessError("operation_changed", "已失败操作必须由新的尝试重新发起")
            value = {**(previous or {}), **fields, "binding": binding}
            operations[key] = value
            task["updated_at"] = time.time()
            connection.execute(
                "UPDATE tasks SET payload=?,updated_at=? WHERE task_id=?",
                (json_text(task), task["updated_at"], self.task_id),
            )
            return value
