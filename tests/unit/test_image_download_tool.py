from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid
from pathlib import Path
from unittest import mock

from chatcopilot.agent.tools.builtin.workspace import images as image_tools
from chatcopilot.agent.tools.workspace_context import bind_workspace_service
from chatcopilot.agent.tools.builtin import workspace_tools
from chatcopilot.agent.tools.registry import discover_tools
from chatcopilot.contracts.tools import ToolContext
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
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
            return_value=image_tools._ImageHttpResponse(
                status=200,
                content_type="image/png",
                content_length=str(len(_PNG)),
                data=_PNG,
            ),
        ) as request_once, bind_workspace_service(_WorkspaceService(self.root)):
            result = workspace_tools._handler_download_image_urls(
                {"urls": ["https://example.com/a.png"]}, ToolContext()
            )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.file_type_hint, "image")
        self.assertEqual(len(result.outputs), 1)
        output = Path(result.outputs[0])
        self.assertTrue(output.is_file())
        self.assertTrue(str(output).startswith(str(self.root)))
        self.assertIn("downloads", result.summary)
        self.assertEqual(output.read_bytes(), _PNG)
        resolved = request_once.call_args.args[0]
        self.assertEqual(resolved.host, "example.com")
        self.assertEqual(resolved.addresses, ("93.184.216.34",))

    def test_rejects_private_ip_url(self) -> None:
        with bind_workspace_service(_WorkspaceService(self.root)), self.assertRaises(RuntimeError) as ctx:
            workspace_tools._handler_download_image_urls(
                {"urls": ["http://127.0.0.1/a.png"]}, ToolContext()
            )
        self.assertIn("没有成功下载任何图片", str(ctx.exception))

    def test_rejects_non_image_response(self) -> None:
        with self._public_dns(), mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
            return_value=image_tools._ImageHttpResponse(
                status=200,
                content_type="text/html",
                data=b"not an image",
            ),
        ), bind_workspace_service(_WorkspaceService(self.root)):
            with self.assertRaises(RuntimeError) as ctx:
                workspace_tools._handler_download_image_urls(
                    {"urls": ["https://example.com/a.html"]}, ToolContext()
                )
        self.assertIn("没有成功下载任何图片", str(ctx.exception))

    def test_rejects_url_userinfo_without_leaking_it(self) -> None:
        credential = "sec" + "ret"
        query = "to" + "ken=private"
        userinfo_url = (
            "https:" + "//user:" + credential + "@" + "example.com/a.png?" + query
        )
        with bind_workspace_service(_WorkspaceService(self.root)), self.assertRaises(
            RuntimeError
        ) as ctx:
            workspace_tools._handler_download_image_urls(
                {"urls": [userinfo_url]},
                ToolContext(),
            )
        error = str(ctx.exception)
        self.assertIn("不允许携带用户信息", error)
        self.assertNotIn(credential, error)
        self.assertNotIn(query, error)

    def test_redirect_to_private_address_is_rejected_before_second_request(self) -> None:
        query = "to" + "ken=secret"
        with self._public_dns(), mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
            return_value=image_tools._ImageHttpResponse(
                status=302,
                location=f"http://127.0.0.1/private.png?{query}",
            ),
        ) as request_once, bind_workspace_service(_WorkspaceService(self.root)):
            with self.assertRaises(RuntimeError) as ctx:
                workspace_tools._handler_download_image_urls(
                    {"urls": ["https://example.com/a.png"]}, ToolContext()
                )
        self.assertEqual(request_once.call_count, 1)
        self.assertIn("拒绝非公网图片地址", str(ctx.exception))
        self.assertNotIn(query, str(ctx.exception))

    def test_fake_ip_dns_uses_independently_resolved_public_address(self) -> None:
        fake_ip = "198.18." + "0.8"
        fake_dns = mock.patch(
            "chatcopilot.agent.tools.builtin.workspace_tools.socket.getaddrinfo",
            return_value=[(None, None, None, None, (fake_ip, 443))],
        )
        with fake_dns, mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._resolve_hostname_via_doh",
            return_value=("93.184.216.34",),
        ) as secure_resolve, mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
            return_value=image_tools._ImageHttpResponse(
                status=200,
                content_type="image/png",
                data=_PNG,
            ),
        ) as request_once, bind_workspace_service(_WorkspaceService(self.root)):
            result = workspace_tools._handler_download_image_urls(
                {"urls": ["https://example.com/a.png"]}, ToolContext()
            )

        self.assertTrue(result.ok, result.error)
        secure_resolve.assert_called_once_with("example.com")
        self.assertEqual(request_once.call_args.args[0].addresses, ("93.184.216.34",))

    def test_non_fake_private_dns_does_not_use_doh_fallback(self) -> None:
        loopback_ip = "127.0." + "0.2"
        private_dns = mock.patch(
            "chatcopilot.agent.tools.builtin.workspace_tools.socket.getaddrinfo",
            return_value=[(None, None, None, None, (loopback_ip, 443))],
        )
        with private_dns, mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._resolve_hostname_via_doh"
        ) as secure_resolve, bind_workspace_service(
            _WorkspaceService(self.root)
        ), self.assertRaises(RuntimeError) as ctx:
            workspace_tools._handler_download_image_urls(
                {"urls": ["https://example.com/a.png"]}, ToolContext()
            )

        secure_resolve.assert_not_called()
        self.assertIn("拒绝非公网图片地址", str(ctx.exception))

    def test_doh_fallback_rejects_private_answer(self) -> None:
        fake_ip = "198.18." + "0.9"
        loopback_ip = "127.0." + "0.3"
        fake_dns = mock.patch(
            "chatcopilot.agent.tools.builtin.workspace_tools.socket.getaddrinfo",
            return_value=[(None, None, None, None, (fake_ip, 443))],
        )
        with fake_dns, mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._resolve_hostname_via_doh",
            return_value=(loopback_ip,),
        ), bind_workspace_service(_WorkspaceService(self.root)), self.assertRaises(
            RuntimeError
        ) as ctx:
            workspace_tools._handler_download_image_urls(
                {"urls": ["https://example.com/a.png"]}, ToolContext()
            )

        self.assertIn("拒绝非公网图片地址", str(ctx.exception))

    def test_doh_resolver_pins_bootstrap_address(self) -> None:
        payload = json.dumps(
            {
                "Status": 0,
                "Question": [{"name": "example.com.", "type": 1}],
                "Answer": [
                    {"name": "example.com.", "type": 1, "data": "93.184.216.34"}
                ],
            }
        ).encode("utf-8")
        response = mock.Mock(status=200)
        response.getheader.return_value = "application/dns-json"
        response.read.side_effect = [payload, b""]
        connection = mock.Mock()
        connection.getresponse.return_value = response

        with mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._PinnedHTTPSConnection",
            return_value=connection,
        ) as pinned:
            addresses = image_tools._resolve_hostname_via_doh("example.com")

        self.assertEqual(addresses, ("93.184.216.34",))
        pinned.assert_called_once_with(
            image_tools._IMAGE_DOH_HOST,
            443,
            image_tools._IMAGE_DOH_BOOTSTRAP_ADDRESSES[0],
        )
        connection.request.assert_called_once_with(
            "GET",
            "/dns-query?name=example.com&type=A",
            headers={
                "Accept": "application/dns-json",
                "User-Agent": image_tools._IMAGE_REQUEST_HEADERS["User-Agent"],
            },
        )
        connection.close.assert_called_once()

    def test_redirect_limit_is_enforced(self) -> None:
        redirects = [
            image_tools._ImageHttpResponse(status=302, location=f"/redirect-{index}")
            for index in range(6)
        ]
        with self._public_dns(), mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
            side_effect=redirects,
        ) as request_once, bind_workspace_service(_WorkspaceService(self.root)):
            with self.assertRaises(RuntimeError) as ctx:
                workspace_tools._handler_download_image_urls(
                    {"urls": ["https://example.com/a.png"]}, ToolContext()
                )
        self.assertEqual(request_once.call_count, 6)
        self.assertIn("重定向超过 5 次", str(ctx.exception))

    def test_mime_must_match_image_signature(self) -> None:
        with self._public_dns(), mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
            return_value=image_tools._ImageHttpResponse(
                status=200,
                content_type="image/jpeg",
                data=_PNG,
            ),
        ), bind_workspace_service(_WorkspaceService(self.root)):
            with self.assertRaises(RuntimeError) as ctx:
                workspace_tools._handler_download_image_urls(
                    {"urls": ["https://example.com/a.jpg"]}, ToolContext()
                )
        self.assertIn("Content-Type 与图片签名不匹配", str(ctx.exception))

    def test_pinned_request_uses_validated_address_and_enforces_size(self) -> None:
        class RawResponse:
            status = 200

            @staticmethod
            def getheader(name: str):
                return {
                    "Content-Type": "image/png",
                    "Content-Length": "101",
                    "Location": "",
                }.get(name)

            @staticmethod
            def read(_size: int = -1) -> bytes:
                raise AssertionError("oversized response must fail before reading")

        connection = mock.Mock()
        connection.getresponse.return_value = RawResponse()
        resolved = image_tools._ResolvedPublicUrl(
            parsed=image_tools.urllib.parse.urlsplit(
                "https://example.com/image.png?variant=large"
            ),
            host="example.com",
            port=443,
            addresses=("93.184.216.34",),
        )
        with mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._PinnedHTTPSConnection",
            return_value=connection,
        ) as pinned, self.assertRaises(ValueError) as ctx:
            image_tools._request_image_once(resolved, max_bytes=100)

        pinned.assert_called_once_with("example.com", 443, "93.184.216.34")
        connection.request.assert_called_once_with(
            "GET",
            "/image.png?variant=large",
            headers=image_tools._IMAGE_REQUEST_HEADERS,
        )
        connection.close.assert_called_once()
        self.assertIn("图片超过大小上限", str(ctx.exception))

    def test_streamed_response_enforces_size_without_content_length(self) -> None:
        class RawResponse:
            status = 200

            def __init__(self) -> None:
                self._chunks = [b"x" * 101, b""]

            @staticmethod
            def getheader(name: str):
                return {"Content-Type": "image/png", "Location": ""}.get(name)

            def read(self, _size: int = -1) -> bytes:
                return self._chunks.pop(0)

        connection = mock.Mock()
        connection.getresponse.return_value = RawResponse()
        resolved = image_tools._ResolvedPublicUrl(
            parsed=image_tools.urllib.parse.urlsplit("http://example.com/image.png"),
            host="example.com",
            port=80,
            addresses=("93.184.216.34",),
        )
        with mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._PinnedHTTPConnection",
            return_value=connection,
        ), self.assertRaises(ValueError) as ctx:
            image_tools._request_image_once(resolved, max_bytes=100)

        connection.close.assert_called_once()
        self.assertIn("图片超过大小上限", str(ctx.exception))

    def test_workspace_capability_exposes_download_image_tool(self) -> None:
        names = {tool.name for tool in discover_tools(tool_packs=("workspace.read_write",))}
        self.assertIn("download_image_urls", names)
        self.assertIn("send_image_urls_to_user", names)


if __name__ == "__main__":
    unittest.main()
