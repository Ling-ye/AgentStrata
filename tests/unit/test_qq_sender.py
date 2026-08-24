from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import unittest
import uuid
from pathlib import Path
from unittest import mock

from chatcopilot.platforms.qq import sender
from chatcopilot.platforms.qq.gateway_health import QQBoundaryError


_PNG = b"\x89PNG\r\n\x1a\n" + b"qq-image"
_TMP_PARENT = Path(__file__).resolve().parents[2] / "scratch_unit_tests"


class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        echo = self.sent[-1]["echo"]
        return json.dumps({"status": "ok", "retcode": 0, "echo": echo})


class QQSenderTests(unittest.TestCase):
    def setUp(self) -> None:
        _TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.root = _TMP_PARENT / f"qq-sender-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.image = self.root / "a.png"
        self.image.write_bytes(_PNG)
        self.text = self.root / "a.txt"
        self.text.write_text("hello", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_private_target_uses_user_id(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CHATCOPILOT_CHAT_KIND": "p2p", "CHATCOPILOT_USER_ID": "10001"},
            clear=False,
        ):
            target = sender._delivery_target_from_env()
        self.assertEqual(target.message_type, "private")
        self.assertEqual(target.id_key, "user_id")
        self.assertEqual(target.id_value, "10001")

    def test_group_target_uses_chat_id(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CHATCOPILOT_CHAT_KIND": "group", "CHATCOPILOT_CHAT_ID": "20002"},
            clear=False,
        ):
            target = sender._delivery_target_from_env()
        self.assertEqual(target.message_type, "group")
        self.assertEqual(target.id_key, "group_id")
        self.assertEqual(target.id_value, "20002")

    def test_image_files_use_onebot_sender(self) -> None:
        with mock.patch.dict(os.environ, {"CHATCOPILOT_USER_ID": "10001"}, clear=False), mock.patch(
            "chatcopilot.platforms.qq.sender._send_images_via_onebot",
            return_value="sent images",
        ) as onebot, mock.patch(
            "chatcopilot.platforms.qq.sender._feishu_send_via_cc_connect"
        ) as cc_send:
            result = sender.send_via_cc_connect([self.image], message="给你")

        self.assertEqual(result, "sent images")
        onebot.assert_called_once()
        cc_send.assert_not_called()

    def test_spoofed_image_is_rejected_before_any_sender(self) -> None:
        spoofed = self.root / "spoofed.png"
        spoofed.write_bytes(b"not really a png")
        with mock.patch(
            "chatcopilot.platforms.qq.sender._send_images_via_onebot"
        ) as onebot, mock.patch(
            "chatcopilot.platforms.qq.sender._feishu_send_via_cc_connect"
        ) as cc_send:
            with self.assertRaises(ValueError):
                sender.send_via_cc_connect([spoofed], message="caption")

        onebot.assert_not_called()
        cc_send.assert_not_called()

    def test_non_image_files_fall_back_to_cc_connect(self) -> None:
        with mock.patch(
            "chatcopilot.platforms.qq.sender._feishu_send_via_cc_connect",
            return_value="cc sent",
        ) as cc_send, mock.patch(
            "chatcopilot.platforms.qq.sender._send_images_via_onebot"
        ) as onebot:
            result = sender.send_via_cc_connect([self.text], message="file")

        self.assertEqual(result, "cc sent")
        cc_send.assert_called_once()
        onebot.assert_not_called()

    def test_oversized_image_fails_before_cc_connect(self) -> None:
        with mock.patch.dict(os.environ, {"QQ_IMAGE_MAX_BYTES": "4"}, clear=False), mock.patch(
            "chatcopilot.platforms.qq.sender._feishu_send_via_cc_connect"
        ) as cc_send:
            with self.assertRaises(ValueError):
                sender.send_via_cc_connect([self.image])
        cc_send.assert_not_called()

    def test_onebot_payload_contains_base64_image_segment(self) -> None:
        ws = _FakeWs()
        target = sender._OneBotTarget("private", "user_id", "10001")

        asyncio.run(
            sender._send_onebot_payload(ws, target, [self.image], "caption", timeout_s=1)
        )

        payload = ws.sent[0]
        self.assertEqual(payload["action"], "send_msg")
        params = payload["params"]
        self.assertEqual(params["message_type"], "private")
        self.assertEqual(params["user_id"], "10001")
        segments = params["message"]
        self.assertEqual(segments[0]["type"], "text")
        self.assertEqual(segments[1]["type"], "image")
        encoded = segments[1]["data"]["file"].removeprefix("base64://")
        self.assertEqual(base64.b64decode(encoded), _PNG)

    def test_multiple_images_use_one_group_send_action(self) -> None:
        second = self.root / "b.png"
        second.write_bytes(_PNG + b"-second")
        ws = _FakeWs()
        target = sender._OneBotTarget("group", "group_id", "20002")

        asyncio.run(
            sender._send_onebot_payload(
                ws,
                target,
                [self.image, second],
                "角色立绘",
                timeout_s=1,
            )
        )

        self.assertEqual(len(ws.sent), 1)
        payload = ws.sent[0]
        self.assertEqual(payload["action"], "send_msg")
        self.assertEqual(payload["params"]["message_type"], "group")
        self.assertEqual(payload["params"]["group_id"], "20002")
        segments = payload["params"]["message"]
        self.assertEqual([segment["type"] for segment in segments], ["text", "image", "image"])

    def test_onebot_text_payload_targets_group(self) -> None:
        ws = _FakeWs()
        target = sender._OneBotTarget("group", "group_id", "20002")

        asyncio.run(sender._send_text_payload(ws, target, "task done", timeout_s=1))

        payload = ws.sent[0]
        self.assertEqual(payload["action"], "send_msg")
        self.assertEqual(payload["params"]["message_type"], "group")
        self.assertEqual(payload["params"]["group_id"], "20002")
        self.assertEqual(payload["params"]["message"][0]["data"]["text"], "task done")

    def test_direct_onebot_sender_rejects_missing_token_before_connect(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "QQ_WS_URL": "ws://127.0.0.1:3001",
                "QQ_ACCESS_TOKEN": "",
            },
            clear=False,
        ), mock.patch("chatcopilot.platforms.qq.sender._run_async") as run_async:
            with self.assertRaises(QQBoundaryError) as caught:
                sender.send_text_via_onebot(
                    message_type="private",
                    id_key="user_id",
                    id_value="10001",
                    text="done",
                )

        self.assertEqual(caught.exception.error_code, "qq_access_token_missing")
        run_async.assert_not_called()

    def test_direct_onebot_sender_rejects_public_url_before_connect(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "QQ_WS_URL": "ws://0.0.0.0:3001",
                "QQ_ACCESS_TOKEN": "x" * 32,
            },
            clear=False,
        ), mock.patch("chatcopilot.platforms.qq.sender._run_async") as run_async:
            with self.assertRaises(QQBoundaryError) as caught:
                sender.send_text_via_onebot(
                    message_type="private",
                    id_key="user_id",
                    id_value="10001",
                    text="done",
                )

        self.assertEqual(
            caught.exception.error_code,
            "qq_websocket_url_not_loopback",
        )
        run_async.assert_not_called()


if __name__ == "__main__":
    unittest.main()
