"""Execute deferred lifecycle intents after the final ACP message is delivered."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Literal, Sequence

from chatcopilot.agent.protocol import DeferredLifecycleIntent
from chatcopilot.external_tools.dev.lifecycle_job import workspace_payload as build_workspace_payload
from chatcopilot.external_tools.dev.lifecycle_tools import (
    execute_finalize_self_update_from_workspace,
)

_JOB_ID_RE = re.compile(r"\bjob_\d{8}_\d{6}_[0-9a-fA-F]{8}\b")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LifecycleExecutionResult:
    status: Literal["skipped", "started", "failed"]
    message: str = ""
    job_id: str = ""
    error: str = ""


class LifecycleBarrierExecutor:
    """Runs narrow lifecycle actions only after final-message delivery succeeds."""

    async def execute(
        self,
        intents: Sequence[DeferredLifecycleIntent],
        *,
        final_text_delivered: bool,
        workspace: object | None = None,
        session_id: str | None = None,
    ) -> LifecycleExecutionResult:
        if not intents:
            return LifecycleExecutionResult(status="skipped")
        if len(intents) > 1:
            return LifecycleExecutionResult(
                status="failed",
                error="only one deferred lifecycle intent is allowed per turn",
            )

        intent = intents[0]
        if intent.requires_final_delivery and not final_text_delivered:
            return LifecycleExecutionResult(
                status="skipped",
                message="final response was not delivered; lifecycle action was not started",
            )
        if intent.name != "finalize_self_update":
            return LifecycleExecutionResult(
                status="failed",
                error=f"unsupported lifecycle intent: {intent.name}",
            )

        if workspace is None:
            return LifecycleExecutionResult(
                status="failed",
                error="workspace is required for deferred self update publication",
            )

        try:
            summary, outputs, _file_type = await asyncio.to_thread(
                execute_finalize_self_update_from_workspace,
                dict(intent.arguments),
                workspace_payload=build_workspace_payload(workspace),
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            return LifecycleExecutionResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

        rendered = "\n".join([summary or "", *[str(item) for item in outputs or []]])
        match = _JOB_ID_RE.search(rendered)
        job_id = match.group(0) if match else ""
        logger.info(
            "deferred_lifecycle_started intent=%s source=%s job_id=%s",
            intent.name,
            intent.source,
            job_id,
        )
        message = (
            f"已开始自动更新重启，任务 ID: {job_id}"
            if job_id
            else "已开始自动更新重启。"
        )
        return LifecycleExecutionResult(
            status="started",
            message=message,
            job_id=job_id,
        )


__all__ = ["LifecycleBarrierExecutor", "LifecycleExecutionResult"]
