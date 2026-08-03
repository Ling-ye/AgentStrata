"""send_files_to_user 工具经注入的 file_sender hook 回传文件的单测。

验证 Agent 层不再直接 import 平台：handler 只通过 contextvar hook 拿到 middleware
注入的 ``FileSender``，并在执行后正确复位上下文。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from chatcopilot.agent.tools.builtin.workspace_tools import TOOLS
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.agent.tools.file_delivery import (
    FileDeliveryResult,
    get_current_file_sender,
)


def _send_files_tool():
    for tool in TOOLS:
        if tool.name == "send_files_to_user":
            return tool
    raise AssertionError("send_files_to_user tool 未注册")


class FileDeliveryHookTests(unittest.TestCase):
    def test_handler_uses_injected_sender(self) -> None:
        calls: list[tuple[list[str], str]] = []

        def fake_sender(files, message):
            calls.append((list(files), message))
            return FileDeliveryResult(
                sent_names=tuple(Path(f).name for f in files),
                sent_paths=tuple(str(f) for f in files),
                message=message,
            )

        executor = ToolExecutor(tools=[_send_files_tool()], file_sender=fake_sender)
        result = executor.execute(
            "send_files_to_user", {"files": ["results/a.csv"], "message": "给你"}
        )
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(calls, [(["results/a.csv"], "给你")])
        self.assertIn("a.csv", result.summary)

    def test_handler_without_sender_fails_clean(self) -> None:
        executor = ToolExecutor(tools=[_send_files_tool()], file_sender=None)
        result = executor.execute("send_files_to_user", {"files": ["a.csv"]})
        self.assertFalse(result.ok)
        self.assertIn("文件回传通道", result.error or "")

    def test_context_is_reset_after_execute(self) -> None:
        executor = ToolExecutor(
            tools=[_send_files_tool()],
            file_sender=lambda files, message: FileDeliveryResult((), (), message),
        )
        executor.execute("send_files_to_user", {"files": ["a.csv"]})
        # 执行结束后 contextvar 必须复位，避免泄漏到后续工具调用。
        self.assertIsNone(get_current_file_sender())


if __name__ == "__main__":
    unittest.main()
