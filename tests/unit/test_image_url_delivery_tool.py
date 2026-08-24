from __future__ import annotations

import json
import shutil
import socket
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.prompt_plan_fixture import prompt_plan

from chatcopilot.agent.backends.codex import CodexAgentBackend
from chatcopilot.agent.backends.session_relay import call_session_relay
from chatcopilot.agent.subagents.selector import is_user_facing
from chatcopilot.agent.tools.builtin.workspace import images as image_tools
from chatcopilot.agent.tools.builtin.workspace_tools import TOOLS
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.agent.tools.file_delivery import FileDeliveryResult
from chatcopilot.contracts.agent_backend import BackendOpenRequest
from chatcopilot.contracts.identity import SessionIdentity
from chatcopilot.core.workspace_runtime import Workspace


_PNG = b"\x89PNG\r\n\x1a\n" + b"delivery" * 8
_TMP_PARENT = Path(__file__).resolve().parents[2] / "scratch_unit_tests"


def _image_delivery_tool():
    for tool in TOOLS:
        if tool.name == "send_image_urls_to_user":
            return tool
    raise AssertionError("send_image_urls_to_user tool 未注册")


class _WorkspaceService:
    def __init__(self, root: Path) -> None:
        self.workspace = Workspace(
            root=root,
            chat_kind="group",
            chat_id="group-test",
            user_id="actor-test",
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


def _png_response() -> image_tools._ImageHttpResponse:
    return image_tools._ImageHttpResponse(
        status=200,
        content_type="image/png",
        content_length=str(len(_PNG)),
        data=_PNG,
    )


class ImageUrlDeliveryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        _TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.root = (_TMP_PARENT / f"image-delivery-{uuid.uuid4().hex}").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace_service = _WorkspaceService(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _public_dns(self):
        real_getaddrinfo = socket.getaddrinfo

        def resolve(host, port, *args, **kwargs):
            if host == "example.com":
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("93.184.216.34", port),
                    )
                ]
            return real_getaddrinfo(host, port, *args, **kwargs)

        return mock.patch(
            "chatcopilot.agent.tools.builtin.workspace_tools.socket.getaddrinfo",
            side_effect=resolve,
        )

    def _executor(self, sender) -> ToolExecutor:
        return ToolExecutor(
            tools=[_image_delivery_tool()],
            file_sender=sender,
            workspace_service=self.workspace_service,
        )

    def test_downloads_multiple_images_and_calls_sender_once(self) -> None:
        calls: list[tuple[list[str], str]] = []

        def sender(files, message):
            paths = tuple(str(path) for path in files)
            calls.append((list(paths), message))
            return FileDeliveryResult(
                sent_names=tuple(Path(path).name for path in paths),
                sent_paths=paths,
                message=message,
            )

        with self._public_dns(), mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
            side_effect=[_png_response(), _png_response()],
        ):
            result = self._executor(sender).execute(
                "send_image_urls_to_user",
                {
                    "urls": [
                        "https://example.com/first.png",
                        "https://example.com/second.png",
                    ],
                    "message": "角色立绘",
                },
            )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "角色立绘")
        self.assertEqual(len(calls[0][0]), 2)
        self.assertEqual(
            result.data,
            {
                "downloaded_count": 2,
                "sent_count": 2,
                "sent_names": [Path(path).name for path in calls[0][0]],
                "failed_count": 0,
            },
        )
        self.assertTrue(all(Path(path).read_bytes() == _PNG for path in result.outputs))

    def test_partial_download_failure_sends_valid_images_and_redacts_query(self) -> None:
        calls: list[list[str]] = []
        query = "to" + "ken=private-value"

        def sender(files, message):
            paths = tuple(str(path) for path in files)
            calls.append(list(paths))
            return FileDeliveryResult(
                sent_names=tuple(Path(path).name for path in paths),
                sent_paths=paths,
                message=message,
            )

        with self._public_dns(), mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
            side_effect=[
                image_tools._ImageHttpResponse(
                    status=200,
                    content_type="text/html",
                    data=b"not-an-image",
                ),
                _png_response(),
            ],
        ):
            result = self._executor(sender).execute(
                "send_image_urls_to_user",
                {
                    "urls": [
                        f"https://example.com/bad?{query}",
                        "https://example.com/good.png",
                    ]
                },
            )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 1)
        self.assertEqual(result.data["failed_count"], 1)
        self.assertNotIn("private-value", result.summary)
        self.assertNotIn(query, result.summary)

    def test_all_downloads_fail_without_calling_sender_or_leaking_query(self) -> None:
        sender = mock.Mock()
        query = "to" + "ken=private-value"
        with self._public_dns(), mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
            return_value=image_tools._ImageHttpResponse(
                status=200,
                content_type="text/html",
                data=b"not-an-image",
            ),
        ):
            result = self._executor(sender).execute(
                "send_image_urls_to_user",
                {"urls": [f"https://example.com/bad?{query}"]},
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "image_download_failed")
        self.assertEqual(result.stage, "download")
        self.assertNotIn("private-value", result.error or "")
        self.assertNotIn(query, result.error or "")
        sender.assert_not_called()

    def test_missing_sender_fails_before_network_or_workspace_download(self) -> None:
        with mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once"
        ) as request_once:
            result = self._executor(None).execute(
                "send_image_urls_to_user",
                {"urls": ["https://example.com/a.png"]},
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "file_delivery_unavailable")
        self.assertEqual(result.stage, "preflight")
        request_once.assert_not_called()
        self.assertFalse((self.root / "downloads" / "images").exists())

    def test_sender_failure_is_unconfirmed_and_not_retried(self) -> None:
        sender = mock.Mock(side_effect=TimeoutError("OneBot timeout"))
        with self._public_dns(), mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
            return_value=_png_response(),
        ):
            result = self._executor(sender).execute(
                "send_image_urls_to_user",
                {"urls": ["https://example.com/a.png"]},
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "image_delivery_unconfirmed")
        self.assertEqual(result.details["delivery_status"], "unknown")
        self.assertEqual(sender.call_count, 1)
        self.assertNotIn("OneBot timeout", result.error or "")

    def test_incomplete_sender_receipt_is_not_reported_as_success(self) -> None:
        sender = mock.Mock(return_value=FileDeliveryResult((), (), ""))
        with self._public_dns(), mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
            return_value=_png_response(),
        ):
            result = self._executor(sender).execute(
                "send_image_urls_to_user",
                {"urls": ["https://example.com/a.png"]},
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "image_delivery_receipt_invalid")
        self.assertNotIn("已发送", result.error or "")

    def test_schema_rejects_more_than_five_urls_before_network(self) -> None:
        sender = mock.Mock()
        with mock.patch(
            "chatcopilot.agent.tools.builtin.workspace.images._request_image_once"
        ) as request_once:
            result = self._executor(sender).execute(
                "send_image_urls_to_user",
                {"urls": [f"https://example.com/{index}.png" for index in range(6)]},
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "tool_input_schema_invalid")
        request_once.assert_not_called()
        sender.assert_not_called()

    def test_tool_is_user_facing_and_codex_gateway_relay_executes_it(self) -> None:
        sent: list[list[str]] = []

        def sender(files, message):
            paths = tuple(str(path) for path in files)
            sent.append(list(paths))
            return FileDeliveryResult(
                sent_names=tuple(Path(path).name for path in paths),
                sent_paths=paths,
                message=message,
            )

        tool = _image_delivery_tool()
        self.assertTrue(is_user_facing(tool))
        executor = self._executor(sender)
        routing = SimpleNamespace(
            code_command="codex exec --model {model} --cd {workdir}",
            code_model="gpt-test",
            code_reasoning_effort="medium",
            code_timeout_seconds=30,
            code_workdir_env="CHATCOPILOT_UNUSED_WORKDIR",
        )
        backend = CodexAgentBackend(
            tool_names={tool.name},
            runtime_config=SimpleNamespace(routing=routing),
            tools=(tool,),
            tool_executor=executor,
        )
        ref = backend.open_session(
            BackendOpenRequest(
                session_id="group-image-delivery",
                prompt_plan=prompt_plan("system"),
                allowed_tool_names=frozenset({tool.name}),
                caller_identity=SessionIdentity(
                    user_id="actor-test",
                    chat_id="group-test",
                    chat_kind="group",
                ),
                options={
                    "workspace_root": self.root,
                    "backend_state_root": self.root / ".state",
                    "role_hint": "user",
                },
            )
        )
        try:
            gateway = json.loads(
                backend.native_session(ref).gateway_config.read_text(encoding="utf-8")
            )
            self.assertEqual(gateway["allowed_tools"], ["send_image_urls_to_user"])
            with self._public_dns(), mock.patch(
                "chatcopilot.agent.tools.builtin.workspace.images._request_image_once",
                return_value=_png_response(),
            ):
                response = call_session_relay(
                    gateway["relay"],
                    {
                        "action": "call_tool",
                        "name": "send_image_urls_to_user",
                        "arguments": {"urls": ["https://example.com/a.png"]},
                    },
                )
            self.assertTrue(response["result"]["ok"])
            self.assertEqual(response["result"]["data"]["sent_count"], 1)
            self.assertEqual(len(sent), 1)
        finally:
            backend.close_session(ref)


if __name__ == "__main__":
    unittest.main()
