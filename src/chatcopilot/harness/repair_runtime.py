"""Trace and workspace composition around the repair service."""
from pathlib import Path
import sqlite3
import uuid
from typing import Any
from chatcopilot.core.private_sqlite import storage_error_details
from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
from chatcopilot.harness.repair_repository import RepairArtifacts
from chatcopilot.harness.workflow import run_task as execute
from chatcopilot.harness.engine.context import HarnessContext
from chatcopilot.harness.integrations.roles import RoleWorkflow
from chatcopilot.harness.integrations.scenarios import RepairScenario, GovernanceScenario
from chatcopilot.harness.artifact_repository import ArtifactRepository


def run_task(store, task_id, verifier, coder, *, committer=None, prepare=None, deliver=None) -> dict[str, Any]:
    from chatcopilot.core.trace_capture import TraceCapture, capture_scope
    from chatcopilot.core.trace_archive import TraceArchive
    capture = TraceCapture({"kind": "harness", "task_id": task_id, "phase": "workflow", "execution_id": uuid.uuid4().hex})
    root = store.root / "jobs" / task_id / "phase-traces"
    pending = {"trace_ref": capture.ref, "capture_state": "recording", "source": capture.source,
               "started_at": capture.started, "finished_at": None, "expires_at": None}
    store.register_trace(task_id, root, pending)
    status = "failed"
    try:
        with capture_scope(capture):
            scenario = GovernanceScenario() if store.get(task_id)["source"].get("kind") == "code_health" else RepairScenario()
            context = HarnessContext(store, task_id, verifier, scenario,
                workspace_factory=lambda task: RepairArtifacts(store.root, task),
                roles_factory=lambda workspace: RoleWorkflow(store, task_id, coder,
                    ArtifactRepository(workspace.directory), workspace.digest), preparer=prepare)
            result = execute(context)
            if result["status"] == "fixed" and deliver is not None:
                # Publication has its own graph and active budget segment.
                deliver()
                result = store.get(task_id)
        status = result["status"]
        return result
    except (sqlite3.Error, OSError) as exc:
        if not isinstance(exc, sqlite3.Error) and not storage_error_details(exc):
            raise
        interrupted = store.interrupt(task_id, storage_error=exc)
        # The worker has stopped; only reconcile the owned worktree identity, never replay a transaction.
        if interrupted.get("worktree"):
            digest = manifest_digest(source_manifest(Path(interrupted["worktree"])))
            interrupted = store.update(task_id, working_digest=digest)
        return interrupted
    finally:
        try:
            store.register_trace(task_id, root, TraceArchive(root).save(capture, status, retained=True))
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Harness workflow trace unavailable")
