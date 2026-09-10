"""Release exact benchmark resources after the Trial process has stopped."""

from __future__ import annotations

from typing import Any, Mapping
import hashlib


def cleanup_environment(lease: Mapping[str, Any] | None) -> None:
    if not lease:
        return
    if lease.get("kind") == "agentbench-fc":
        from chatcopilot.evals.adapters.agentbench import Controller

        session = lease.get("session_id")
        if type(session) is not int or session < 0:
            raise ValueError("invalid AgentBench resource identity")
        client = Controller()
        if lease.get("controller_fingerprint") != hashlib.sha256(client.url.encode()).hexdigest():
            client.client.close()
            raise ValueError("AgentBench controller changed before cleanup")
        client.session_id = session
        client.close()
    elif lease.get("kind") == "swe-bench":
        from chatcopilot.evals.adapters.swebench_runtime import cleanup_container

        cleanup_container(str(lease.get("container") or ""))
    else:
        raise ValueError("unrecognized benchmark environment lease")
