"""Bot-local, one-shot approval records for open-source adapter integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid
from typing import Any

from chatcopilot.contracts.adapter_approval import (
    AdapterApprovalEnvelope,
    validate_adapter_approval,
)
from chatcopilot.core.bot_paths import resolve_bot_spec_path
from chatcopilot.project import ENV_PREFIX


class AdapterApprovalStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    @classmethod
    def for_bot(cls, bot_spec_path: Path) -> "AdapterApprovalStore":
        return cls(bot_spec_path.resolve().parent / ".agentstrata" / "adapter-approvals")

    def approve(
        self,
        *,
        envelope: AdapterApprovalEnvelope,
        candidate_digest: str,
        approved_by: str,
    ) -> dict[str, Any]:
        errors = validate_adapter_approval(envelope)
        if errors:
            raise ValueError("; ".join(errors))
        if candidate_digest != envelope.candidate_digest:
            raise ValueError("candidate_digest does not match the adapter approval envelope")
        identity = approved_by.strip()
        if not identity:
            raise PermissionError("adapter approval requires a stable owner user_id")

        record = {
            "schema_version": 1,
            "candidate_digest": candidate_digest,
            "envelope": envelope.canonical_payload(),
            "approved_by": identity,
            "approved_at": time.time(),
        }
        record_path, consumed_path = self._paths(candidate_digest)
        self._write_json(record_path, record)
        consumed_path.unlink(missing_ok=True)
        return dict(record)

    def consume(
        self,
        *,
        envelope: AdapterApprovalEnvelope,
        candidate_digest: str,
        consumed_by: str,
    ) -> dict[str, Any]:
        errors = validate_adapter_approval(envelope)
        if errors:
            raise ValueError("; ".join(errors))
        if candidate_digest != envelope.candidate_digest:
            raise PermissionError("adapter request differs from the approved digest")
        identity = consumed_by.strip()
        if not identity:
            raise PermissionError("adapter delegation requires a stable owner user_id")

        record_path, consumed_path = self._paths(candidate_digest)
        record = self._read_json(record_path)
        if record is None:
            raise PermissionError("adapter approval record not found")
        if record.get("envelope") != envelope.canonical_payload():
            raise PermissionError("adapter request differs from the approved envelope")
        if str(record.get("approved_by") or "") != identity:
            raise PermissionError("adapter approval belongs to a different owner")

        consumed = {
            "schema_version": 1,
            "candidate_digest": candidate_digest,
            "consumed_by": identity,
            "consumed_at": time.time(),
        }
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(
                consumed_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise PermissionError("adapter approval has already been consumed") from exc
        try:
            os.write(
                fd,
                (json.dumps(consumed, ensure_ascii=False, sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
            )
        finally:
            os.close(fd)
        return dict(record)

    def _paths(self, candidate_digest: str) -> tuple[Path, Path]:
        if not candidate_digest.startswith("sha256:") or len(candidate_digest) != 71:
            raise ValueError("candidate_digest must be sha256:<64 lowercase hex>")
        digest = candidate_digest.removeprefix("sha256:")
        if any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("candidate_digest must be sha256:<64 lowercase hex>")
        return self.root / f"{digest}.json", self.root / f"{digest}.consumed.json"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid adapter approval record") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid adapter approval record")
        return payload

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def resolve_adapter_bot_spec(value: str | None = None) -> Path:
    if value:
        path = resolve_bot_spec_path(value)
        if path.is_file():
            return path
        raise FileNotFoundError(f"BotSpec not found: {value}")
    for env_name in (f"{ENV_PREFIX}_SOURCE_BOT_SPEC", f"{ENV_PREFIX}_BOT_SPEC"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            path = Path(raw).expanduser().resolve()
            if path.is_file():
                return path
    bot_id = os.environ.get(f"{ENV_PREFIX}_BOT_ID", "").strip()
    if bot_id:
        path = resolve_bot_spec_path(bot_id)
        if path.is_file():
            return path
    raise RuntimeError("cannot resolve BotSpec for adapter approval")


__all__ = ["AdapterApprovalStore", "resolve_adapter_bot_spec"]
