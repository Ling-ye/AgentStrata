from __future__ import annotations

import os
import shutil
import unittest
import uuid
from pathlib import Path
from unittest import mock

from chatcopilot.agent.tools.workspace_context import bind_workspace_service
from chatcopilot.agent.tools.builtin import workspace_tools
from chatcopilot.agent.tools.registry import discover_tools
from chatcopilot.core.workspace_runtime import Workspace


_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32
_TMP_PARENT = Path(__file__).resolve().parents[2] / "scratch_unit_tests"


class _WorkspaceService:
    def __init__(self, root: Path) -> None:
        self.workspace = Workspace(
            root=root,
            chat_kind="p2p",
            chat_id=None,
            user_id="u1",
        ).ensure()

    def resolve_workspace(self, *, create: bool = True) -> Workspace:
        return self.workspace

    def resolve_workspace_root(self, workspace=None) -> Path:
        return self.workspace.root

    def cleanup_workspace(self, workspace) -> None:
        return None

    def describe_workspace(self, workspace) -> str:
        return f"workspace={workspace.root}"

    def list_workspace_inventories(self, root: Path) -> list:
        return []


class _FakeResponse:
    def __init__(self, data: bytes, *, content_type: str = "image/png") -> None:
        self._data = data
        self._offset = 0
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
        }

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._data):
            return b""
        if size < 0:
            size = len(self._data) - self._offset
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class DownloadImageUrlsTests(unittest.TestCase):
    def setUp(self) -> None:
        _TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.root = (_TMP_PARENT / f"image-download-{uuid.uuid4().hex}").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "CHATCOPILOT_WORKSPACE_ROOT": str(self.root),
                "CHATCOPILOT_CHAT_KIND": "p2p",
                "CHATCOPILOT_USER_ID": "u1",
                "CHATCOPILOT_CHAT_ID": "",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _public_dns(self):
        return mock.patch(
            "chatcopilot.agent.tools.builtin.workspace_tools.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 0))],
        )

    def test_downloads_public_png_into_workspace(self) -> None:
        with self._public_dns(), mock.patch(
            "chatcopilot.agent.tools.builtin.workspace_tools.urllib.request.urlopen",
            return_value=_FakeResponse(_PNG),
        ), bind_workspace_service(_WorkspaceService(self.root)):
            summary, outputs, file_type = workspace_tools._handler_download_image_urls(
                {"urls": ["https://example.com/a.png"]}
            )

        self.assertEqual(file_type, "image")
        self.assertEqual(len(outputs), 1)
        output = Path(outputs[0])
        self.assertTrue(output.is_file())
        self.assertTrue(str(output).startswith(str(self.root)))
        self.assertIn("downloads", summary)
        self.assertEqual(output.read_bytes(), _PNG)

    def test_rejects_private_ip_url(self) -> None:
        with bind_workspace_service(_WorkspaceService(self.root)), self.assertRaises(RuntimeError) as ctx:
            workspace_tools._handler_download_image_urls(
                {"urls": ["http://127.0.0.1/a.png"]}
            )
        self.assertIn("没有成功下载任何图片", str(ctx.exception))

    def test_rejects_non_image_response(self) -> None:
        with self._public_dns(), mock.patch(
            "chatcopilot.agent.tools.builtin.workspace_tools.urllib.request.urlopen",
            return_value=_FakeResponse(b"not an image", content_type="text/html"),
        ), bind_workspace_service(_WorkspaceService(self.root)):
            with self.assertRaises(RuntimeError) as ctx:
                workspace_tools._handler_download_image_urls(
                    {"urls": ["https://example.com/a.html"]}
                )
        self.assertIn("没有成功下载任何图片", str(ctx.exception))

    def test_workspace_capability_exposes_download_image_tool(self) -> None:
        names = {tool.name for tool in discover_tools(tool_packs=("workspace.read_write",))}
        self.assertIn("download_image_urls", names)


if __name__ == "__main__":
    unittest.main()
