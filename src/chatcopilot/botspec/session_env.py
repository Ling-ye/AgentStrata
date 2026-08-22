"""Bot hook adapter for the private cross-process session handoff."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping

from chatcopilot.contracts.identity import SessionIdentity
from chatcopilot.core.session_env_store import (
    MAX_SESSION_ATTESTATIONS,
    SESSION_ATTESTATION_TTL_NS,
    read_session_identity,
    write_session_env,
)


def build_session_env_values(
    identity: SessionIdentity,
    *,
    hook_event: str | None = None,
    transport_user_id: str | None = None,
    hook_content: str | None = None,
) -> dict[str, str]:
    """Project parsed hook identity and optional message evidence into store values."""

    values = {
        "CHATCOPILOT_USER_ID": identity.user_id or "",
        "CHATCOPILOT_CHAT_ID": identity.chat_id or "",
        "CHATCOPILOT_CHAT_KIND": identity.chat_kind or "",
        "CHATCOPILOT_USER_NAME": identity.user_name or "",
    }
    if (hook_event or "").strip() == "message.received":
        values.update(
            {
                "CHATCOPILOT_TRANSPORT_HOOK_EVENT": "message.received",
                "CHATCOPILOT_TRANSPORT_USER_ID": (transport_user_id or "").strip(),
                "CHATCOPILOT_TRANSPORT_CONTENT_SHA256": hashlib.sha256(
                    (hook_content or "").strip().encode("utf-8")
                ).hexdigest(),
            }
        )
    return values


def write_private_session_env(
    *,
    directory: str | Path,
    session_key: str,
    values: Mapping[str, str],
    queue_transport: bool = True,
    max_attestations: int = MAX_SESSION_ATTESTATIONS,
    ttl_ns: int = SESSION_ATTESTATION_TTL_NS,
) -> Path:
    """Write one hook refresh through the core-owned secure store."""

    return write_session_env(
        directory=directory,
        session_key=session_key,
        values=values,
        queue_transport=queue_transport,
        max_attestations=max_attestations,
        ttl_ns=ttl_ns,
    )


def read_private_session_env(*, directory: str | Path, session_key: str) -> dict[str, str]:
    return read_session_identity(directory=directory, session_key=session_key)


__all__ = [
    "build_session_env_values",
    "read_private_session_env",
    "write_private_session_env",
]
