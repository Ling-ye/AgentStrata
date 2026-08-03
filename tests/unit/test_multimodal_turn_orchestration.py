from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from typing import Any

from acp import PromptResponse
from acp.schema import ImageContentBlock

from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.middleware.acp.attachment_pipeline import ExtractedPrompt
from chatcopilot.middleware.acp.turn_orchestrator import AcpTurnOrchestrator
from chatcopilot.middleware.acp.turn_pipeline import TurnContext

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _Connection:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def session_update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class _Session:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.pending_image_resources = ()
        self.pending_image_names = ()
        self.exchanges: list[tuple[str, str]] = []

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        self.exchanges.append((user_text, assistant_text))

    def message_count(self) -> int:
        return len(self.exchanges) * 2


class _Host:
    def __init__(self) -> None:
        self._conn = _Connection()
        self.agent_calls: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    async def _ensure_agent_session(
        self,
        _session_id: str,
        session: _Session,
    ) -> _Session:
        return session

    async def _run_agent_turn(self, *args: Any, **kwargs: Any) -> PromptResponse:
        self.agent_calls.append({"args": args, "kwargs": kwargs})
        return PromptResponse(stop_reason="end_turn")

    def _finish_turn_task(self, _turn_task: Any, **kwargs: Any) -> None:
        self.finished.append(kwargs)

    def _cancel_attachment_ack(self, session_id: str) -> None:
        self.cancelled.append(session_id)

    def _schedule_attachment_ack(self, **_kwargs: Any) -> None:
        return None


def _text_update(text: str) -> dict[str, str]:
    return {"text": text}


class MultimodalTurnOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        workspace = Workspace(
            root=Path(self._tmp.name),
            chat_kind="p2p",
            chat_id="test-chat",
            user_id="test-user",
        )
        self.session = _Session(workspace)
        self.host = _Host()
        self.orchestrator = AcpTurnOrchestrator(
            self.host,
            platform_type="qq",
            has_image_inputs=True,
            has_role_matrix=False,
            has_user_files_pipeline=True,
            has_private_space_inventory=False,
            update_text=_text_update,
            recover_workspace=lambda *_args: None,
            refresh_system_prompt=lambda _session: None,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _image_block() -> ImageContentBlock:
        return ImageContentBlock(
            type="image",
            data=base64.b64encode(_PNG_BYTES).decode("ascii"),
            mimeType="image/png",
        )

    def _turn(
        self,
        *,
        user_text: str,
        raw_prompt: list[object],
    ) -> TurnContext:
        return TurnContext(
            session_id="sid",
            session=self.session,
            user_text=user_text,
            message_id="mid",
            metadata={
                "raw_prompt": raw_prompt,
                "prompt_parts": ExtractedPrompt(
                    text=user_text,
                    resource_names=[],
                ),
            },
        )

    async def test_image_only_is_staged_then_consumed_once(self) -> None:
        upload_turn = self._turn(user_text="", raw_prompt=[self._image_block()])

        upload_outcome = await self.orchestrator._session_materialization(upload_turn)

        self.assertTrue(upload_outcome.stop)
        self.assertEqual(upload_outcome.reason, "images_staged")
        self.assertEqual(len(self.session.pending_image_resources), 1)
        self.assertEqual(self.host.agent_calls, [])

        instruction_turn = self._turn(
            user_text="描述这张图片",
            raw_prompt=[],
        )
        instruction_outcome = await self.orchestrator._session_materialization(
            instruction_turn
        )
        self.assertFalse(instruction_outcome.stop)
        self.assertEqual(len(instruction_turn.metadata["task_resources"]), 1)
        self.assertEqual(len(self.session.pending_image_resources), 1)

        await self.orchestrator._execution(instruction_turn)

        self.assertEqual(self.session.pending_image_resources, ())
        self.assertEqual(
            len(self.host.agent_calls[0]["kwargs"]["task_resources"]),
            1,
        )

        later_turn = self._turn(user_text="继续", raw_prompt=[])
        later_outcome = await self.orchestrator._session_materialization(later_turn)
        self.assertFalse(later_outcome.stop)
        await self.orchestrator._execution(later_turn)
        self.assertNotIn("task_resources", self.host.agent_calls[1]["kwargs"])

    async def test_image_and_text_are_forwarded_in_same_turn(self) -> None:
        turn = self._turn(
            user_text="这是什么？",
            raw_prompt=[self._image_block()],
        )

        outcome = await self.orchestrator._session_materialization(turn)

        self.assertFalse(outcome.stop)
        self.assertEqual(len(turn.metadata["task_resources"]), 1)
        self.assertEqual(self.host.agent_calls, [])

    async def test_disabled_feature_rejects_inline_image_without_writing(self) -> None:
        disabled = AcpTurnOrchestrator(
            self.host,
            platform_type="qq",
            has_image_inputs=False,
            has_role_matrix=False,
            has_user_files_pipeline=True,
            has_private_space_inventory=False,
            update_text=_text_update,
            recover_workspace=lambda *_args: None,
            refresh_system_prompt=lambda _session: None,
        )
        turn = self._turn(
            user_text="分析图片",
            raw_prompt=[self._image_block()],
        )

        outcome = await disabled._session_materialization(turn)

        self.assertTrue(outcome.stop)
        self.assertEqual(outcome.reason, "image_inputs_disabled")
        self.assertFalse((self.session.workspace.attachments / "images").exists())
        self.assertEqual(self.host.agent_calls, [])


if __name__ == "__main__":
    unittest.main()
