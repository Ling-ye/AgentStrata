from __future__ import annotations

import json
import sys
from dataclasses import asdict

import pytest

from chatcopilot.evals.qq_live_driver import (
    MAX_IMAGE_BYTES,
    QQEvalDriverError,
    QQLiveEvaluationDriver,
    QQTransportObservation,
    execute_qq_live_case,
    preflight_qq_live,
)

TOKEN = "t" * 48
SENDER = "12345001"
BOT = "12345002"
GROUP = "12345003"
KEY = b"test-evidence-key-that-is-not-secret"


def _env(**overrides: str) -> dict[str, str]:
    values = {
        "CHATCOPILOT_EVAL_QQ_ENABLED": "true",
        "CHATCOPILOT_EVAL_QQ_SENDER_WS_URL": "ws://127.0.0.1:33001",
        "CHATCOPILOT_EVAL_QQ_SENDER_ACCESS_TOKEN": TOKEN,
        "CHATCOPILOT_EVAL_QQ_SENDER_ID": SENDER,
        "CHATCOPILOT_EVAL_QQ_GROUP_ID": GROUP,
    }
    values.update(overrides)
    return values


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _observe(self, scenario: str, kwargs: dict[str, object]) -> QQTransportObservation:
        self.calls.append((scenario, kwargs))
        text = str(kwargs.get("text") or kwargs.get("caption") or "")
        nonce = text.split("测评 nonce: ", 1)[1].splitlines()[0]
        return QQTransportObservation(
            message_id=f"raw-message-id-{scenario}",
            reply_text=f"{nonce}\nANSWER=AS-2026-0817",
        )

    def private_text(self, **kwargs: object) -> QQTransportObservation:
        return self._observe("private_text", kwargs)

    def group_at_text(self, **kwargs: object) -> QQTransportObservation:
        return self._observe("group_at_text", kwargs)

    def group_image(self, **kwargs: object) -> QQTransportObservation:
        return self._observe("group_image", kwargs)


def _driver(transport: object | None = None) -> QQLiveEvaluationDriver:
    return QQLiveEvaluationDriver.from_env(
        bot_id=BOT,
        whitelist_ids=[SENDER],
        confirm_external_write=True,
        env=_env(),
        evidence_key=KEY,
        transport=transport,  # type: ignore[arg-type]
    )


def test_preflight_is_side_effect_free_and_receipt_never_exposes_secrets_or_ids() -> None:
    receipt = preflight_qq_live(
        bot_id=BOT,
        whitelist_ids=[SENDER],
        confirm_external_write=True,
        env=_env(),
        evidence_key=KEY,
    )
    serialized = repr(asdict(receipt))
    assert receipt.ok is True
    assert receipt.sender_allowlisted is True
    for sensitive in (TOKEN, SENDER, BOT, GROUP):
        assert sensitive not in serialized
    assert len(receipt.token_hmac) == 64
    assert len(receipt.endpoint_sha256) == 64


@pytest.mark.parametrize(
    ("env", "bot_id", "allowlist", "confirmed", "code"),
    [
        ({}, BOT, [SENDER], True, "qq_eval_configuration_missing"),
        (_env(CHATCOPILOT_EVAL_QQ_ENABLED="false"), BOT, [SENDER], True, "qq_eval_disabled"),
        (_env(), BOT, [SENDER], False, "external_write_confirmation_required"),
        (
            _env(CHATCOPILOT_EVAL_QQ_SENDER_WS_URL="ws://0.0.0.0:33001"),
            BOT,
            [SENDER],
            True,
            "qq_eval_qq_websocket_url_not_loopback",
        ),
        (
            _env(CHATCOPILOT_EVAL_QQ_SENDER_ACCESS_TOKEN="weak"),
            BOT,
            [SENDER],
            True,
            "qq_eval_qq_access_token_invalid",
        ),
        (_env(), BOT, [], True, "qq_eval_sender_not_allowlisted"),
        (_env(), SENDER, [SENDER], True, "qq_eval_sender_matches_bot"),
    ],
)
def test_preflight_fails_closed_with_stable_redacted_classification(
    env: dict[str, str],
    bot_id: str,
    allowlist: list[str],
    confirmed: bool,
    code: str,
) -> None:
    with pytest.raises(QQEvalDriverError) as raised:
        preflight_qq_live(
            bot_id=bot_id,
            whitelist_ids=allowlist,
            confirm_external_write=confirmed,
            env=env,
            evidence_key=KEY,
        )
    assert raised.value.failure_class == "configuration_invalid"
    assert raised.value.code == code
    error = str(raised.value)
    for sensitive in (TOKEN, SENDER, BOT, GROUP):
        assert sensitive not in error


def test_all_three_scenarios_use_only_preflighted_targets_and_return_digests() -> None:
    transport = RecordingTransport()
    driver = _driver(transport)
    private = driver.run_private_text(prompt="请原样确认", expected_text="AS-2026-0817")
    group = driver.run_group_at_text(prompt="请原样确认", expected_text="AS-2026-0817")
    png = b"\x89PNG\r\n\x1a\n" + b"fixture"
    image = driver.run_group_image(
        prompt="识别图片编号",
        image=png,
        media_type="image/png",
        expected_text="AS-2026-0817",
    )

    assert [item.scenario for item in (private, group, image)] == [
        "private_text",
        "group_at_text",
        "group_image",
    ]
    assert all(item.status == "observed" for item in (private, group, image))
    assert all(item.nonce_matched is True for item in (private, group, image))
    assert all(item.answer_checked is True for item in (private, group, image))
    assert all(item.answer_matched is True for item in (private, group, image))
    assert image.image_bytes == len(png)
    assert image.image_sha256
    for scenario, kwargs in transport.calls:
        assert kwargs["endpoint"] == _env()["CHATCOPILOT_EVAL_QQ_SENDER_WS_URL"]
        assert kwargs["access_token"] == TOKEN
        if scenario == "private_text":
            assert kwargs["recipient_id"] == BOT
            assert "group_id" not in kwargs
        else:
            assert kwargs["bot_id"] == BOT
            assert kwargs["group_id"] == GROUP
        outbound = str(kwargs.get("text") or kwargs.get("caption") or "")
        assert "ANSWER=<你的答案>" in outbound
        assert "AS-2026-0817" not in outbound

    serialized = repr([asdict(private), asdict(group), asdict(image)])
    for sensitive in (TOKEN, SENDER, BOT, GROUP, "raw-message-id"):
        assert sensitive not in serialized


def test_default_transport_never_connects_and_is_classified_as_infrastructure() -> None:
    driver = _driver()
    with pytest.raises(QQEvalDriverError) as raised:
        driver.run_private_text(prompt="test")
    assert raised.value.failure_class == "infrastructure_error"
    assert raised.value.code == "qq_live_transport_not_configured"


class BadEvidenceTransport(RecordingTransport):
    def private_text(self, **kwargs: object) -> QQTransportObservation:
        self.calls.append(("private_text", kwargs))
        return QQTransportObservation(message_id="message-1", reply_text="no matching nonce")


class EmptyReplyTransport(RecordingTransport):
    def private_text(self, **kwargs: object) -> QQTransportObservation:
        self.calls.append(("private_text", kwargs))
        return QQTransportObservation(message_id="message-1", reply_text="")


def test_trusted_reply_mismatch_returns_digest_only_observation() -> None:
    driver = _driver(BadEvidenceTransport())
    receipt = driver.run_private_text(prompt="test", expected_text="answer")

    assert receipt.status == "observed"
    assert receipt.nonce_matched is False
    assert receipt.answer_checked is True
    assert receipt.answer_matched is False
    persisted = repr(asdict(receipt))
    assert "no matching nonce" not in persisted
    assert all(value != "answer" for value in asdict(receipt).values())


def test_transport_without_a_trusted_reply_remains_error() -> None:
    with pytest.raises(QQEvalDriverError) as raised:
        _driver(EmptyReplyTransport()).run_private_text(prompt="test")

    assert raised.value.failure_class == "evidence_invalid"
    assert raised.value.code == "qq_live_trusted_reply_missing"


class DeterministicReplyTransport(RecordingTransport):
    def __init__(self, reply_template: str) -> None:
        super().__init__()
        self.reply_template = reply_template

    def group_image(self, **kwargs: object) -> QQTransportObservation:
        self.calls.append(("group_image", kwargs))
        caption = str(kwargs["caption"])
        nonce = caption.split("测评 nonce: ", 1)[1].splitlines()[0]
        return QQTransportObservation(
            message_id="raw-image-message-id",
            reply_text=self.reply_template.format(nonce=nonce),
        )


@pytest.mark.parametrize(
    ("reply_template", "nonce_matched", "answer_matched"),
    [
        ("{nonce}\nANSWER=3", True, True),
        ("{nonce}\nANSWER=30", True, False),
        ("{nonce}\nANSWER=不是3", True, False),
        ("{nonce}\nANSWER=3\nANSWER=3", True, False),
        ("trusted reply without nonce\nANSWER=3", False, True),
    ],
)
def test_image_answer_protocol_is_exact_and_non_self_proving(
    reply_template: str,
    nonce_matched: bool,
    answer_matched: bool,
) -> None:
    transport = DeterministicReplyTransport(reply_template)
    receipt = _driver(transport).run_group_image(
        prompt="数出蓝色圆形数量",
        image=b"\x89PNG\r\n\x1a\nfixture",
        media_type="image/png",
        expected_text="3",
    )

    assert receipt.nonce_matched is nonce_matched
    assert receipt.answer_checked is True
    assert receipt.answer_matched is answer_matched
    persisted = asdict(receipt)
    assert "expected_answer" not in persisted
    assert "3" not in persisted.values()


def test_message_count_text_image_and_timeout_limits_are_hard() -> None:
    transport = RecordingTransport()
    driver = _driver(transport)
    driver.run_private_text(prompt="one")
    driver.run_private_text(prompt="two")
    driver.run_private_text(prompt="three")
    with pytest.raises(QQEvalDriverError, match="limited to 3") as count_error:
        driver.run_private_text(prompt="four")
    assert count_error.value.failure_class == "configuration_invalid"

    with pytest.raises(QQEvalDriverError) as text_error:
        _driver(transport).run_private_text(prompt="x" * 1_001)
    assert text_error.value.code == "qq_eval_message_too_large"

    with pytest.raises(QQEvalDriverError) as image_error:
        _driver(transport).run_group_image(
            prompt="image",
            image=b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES,
            media_type="image/png",
            expected_text="answer",
        )
    assert image_error.value.code == "qq_eval_image_too_large"

    with pytest.raises(QQEvalDriverError) as timeout_error:
        QQLiveEvaluationDriver.from_env(
            bot_id=BOT,
            whitelist_ids=[SENDER],
            confirm_external_write=True,
            env=_env(),
            evidence_key=KEY,
            timeout_seconds=61,
        )
    assert timeout_error.value.code == "qq_eval_timeout_out_of_range"


class ExplodingTransport(RecordingTransport):
    def private_text(self, **kwargs: object) -> QQTransportObservation:
        del kwargs
        raise RuntimeError("do not leak " + TOKEN + SENDER)


def test_transport_error_is_normalized_without_leaking_original_message() -> None:
    driver = _driver(ExplodingTransport())
    with pytest.raises(QQEvalDriverError) as raised:
        driver.run_private_text(prompt="test")
    assert raised.value.failure_class == "infrastructure_error"
    assert raised.value.code == "qq_live_transport_failed"
    assert TOKEN not in str(raised.value)
    assert SENDER not in str(raised.value)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.frames: list[str] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        payload = json.loads(raw)
        self.sent.append(payload)
        params = payload["params"]
        text = "".join(
            segment.get("data", {}).get("text", "")
            for segment in params["message"]
            if segment.get("type") == "text"
        )
        nonce = text.split("测评 nonce: ", 1)[1].splitlines()[0]
        message_type = params["message_type"]
        event: dict[str, object] = {
            "post_type": "message",
            "message_type": message_type,
            "user_id": int(BOT),
            "message": [
                {
                    "type": "text",
                    "data": {"text": f"{nonce}\nANSWER=AS-2026-0817"},
                }
            ],
        }
        if message_type == "group":
            event["group_id"] = int(GROUP)
        # Deliver the reply before the action response to prove the exchange
        # waits for and correlates both pieces of evidence.
        self.frames.extend(
            [
                json.dumps(event),
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "echo": payload["echo"],
                        "data": {"message_id": "onebot-raw-message-id"},
                    }
                ),
            ]
        )

    async def recv(self) -> str:
        return self.frames.pop(0)

    async def close(self) -> None:
        self.closed = True


class FakeWebsocketsModule:
    def __init__(self) -> None:
        self.connections: list[FakeWebSocket] = []
        self.connect_calls: list[tuple[str, dict[str, object]]] = []

    async def connect(self, endpoint: str, **kwargs: object) -> FakeWebSocket:
        self.connect_calls.append((endpoint, kwargs))
        websocket = FakeWebSocket()
        self.connections.append(websocket)
        return websocket


@pytest.mark.parametrize(
    "scenario",
    ["private_text", "group_at_text", "group_image"],
)
def test_real_onebot_transport_uses_authenticated_loopback_and_trusted_reply(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    fake_websockets = FakeWebsocketsModule()
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets)
    image = b"\x89PNG\r\n\x1a\nfixture" if scenario == "group_image" else b""
    media_type = "image/png" if image else ""

    receipt = execute_qq_live_case(
        scenario=scenario,  # type: ignore[arg-type]
        bot_id=BOT,
        whitelist_ids=[SENDER],
        confirm_external_write=True,
        env=_env(),
        prompt="请完成测评",
        expected_text="AS-2026-0817",
        image=image,
        media_type=media_type,
        evidence_key=KEY,
    )

    assert receipt.status == "observed"
    assert receipt.nonce_matched is True
    assert receipt.answer_matched is True
    assert len(fake_websockets.connect_calls) == 1
    endpoint, connect_kwargs = fake_websockets.connect_calls[0]
    assert endpoint == "ws://127.0.0.1:33001"
    assert connect_kwargs["additional_headers"] == [("Authorization", f"Bearer {TOKEN}")]
    websocket = fake_websockets.connections[0]
    assert websocket.closed is True
    params = websocket.sent[0]["params"]
    assert params["message_type"] == ("private" if scenario == "private_text" else "group")
    if scenario == "private_text":
        assert params["user_id"] == BOT
    else:
        assert params["group_id"] == GROUP
        assert params["message"][0] == {"type": "at", "data": {"qq": BOT}}
    if scenario == "group_image":
        image_segments = [item for item in params["message"] if item.get("type") == "image"]
        assert len(image_segments) == 1
        assert image_segments[0]["data"]["file"].startswith("base64://")

    persisted = repr(asdict(receipt))
    for sensitive in (TOKEN, SENDER, BOT, GROUP, "onebot-raw-message-id"):
        assert sensitive not in persisted


@pytest.mark.parametrize(
    "mutation",
    ["unconfirmed", "weak_token", "public_endpoint", "oversized_image"],
)
def test_execute_case_rejects_before_network_on_invalid_boundary_or_payload(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    fake_websockets = FakeWebsocketsModule()
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets)
    env = _env()
    confirmed = True
    image = b"\x89PNG\r\n\x1a\nfixture"
    if mutation == "unconfirmed":
        confirmed = False
    elif mutation == "weak_token":
        env["CHATCOPILOT_EVAL_QQ_SENDER_ACCESS_TOKEN"] = "weak"
    elif mutation == "public_endpoint":
        env["CHATCOPILOT_EVAL_QQ_SENDER_WS_URL"] = "ws://198.51.100.10:3001"
    else:
        image = b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES

    with pytest.raises(QQEvalDriverError):
        execute_qq_live_case(
            scenario="group_image",
            bot_id=BOT,
            whitelist_ids=[SENDER],
            confirm_external_write=confirmed,
            env=env,
            prompt="请完成测评",
            expected_text="AS-2026-0817",
            image=image,
            media_type="image/png",
            evidence_key=KEY,
        )
    assert fake_websockets.connect_calls == []


def test_onebot_transport_ignores_wrong_identity_but_captures_trusted_nonce_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_websockets = FakeWebsocketsModule()

    class NoisyWebSocket(FakeWebSocket):
        async def send(self, raw: str) -> None:
            await super().send(raw)
            good_reply, ack = self.frames
            good = json.loads(good_reply)
            wrong_sender = {**good, "user_id": 99999999}
            wrong_group = {**good, "group_id": 99999998}
            wrong_nonce = {
                **good,
                "message": [
                    {
                        "type": "text",
                        "data": {"text": "trusted reply without nonce\nANSWER=AS-2026-0817"},
                    }
                ],
            }
            self.frames = [
                json.dumps(wrong_sender),
                json.dumps(wrong_group),
                json.dumps(wrong_nonce),
                good_reply,
                ack,
            ]

    async def connect(endpoint: str, **kwargs: object) -> NoisyWebSocket:
        fake_websockets.connect_calls.append((endpoint, kwargs))
        websocket = NoisyWebSocket()
        fake_websockets.connections.append(websocket)
        return websocket

    fake_websockets.connect = connect  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets)
    receipt = execute_qq_live_case(
        scenario="group_at_text",
        bot_id=BOT,
        whitelist_ids=[SENDER],
        confirm_external_write=True,
        env=_env(),
        prompt="请完成测评",
        expected_text="AS-2026-0817",
        evidence_key=KEY,
    )
    assert receipt.status == "observed"
    assert receipt.nonce_matched is False
    assert receipt.answer_checked is True
    assert receipt.answer_matched is True


def test_onebot_transport_without_trusted_reply_remains_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_websockets = FakeWebsocketsModule()

    class NoTrustedReplyWebSocket(FakeWebSocket):
        async def send(self, raw: str) -> None:
            await super().send(raw)
            reply, ack = self.frames
            wrong_sender = {**json.loads(reply), "user_id": 99999999}
            self.frames = [json.dumps(wrong_sender), ack]

        async def recv(self) -> str:
            if not self.frames:
                raise TimeoutError("no trusted bot reply")
            return await super().recv()

    async def connect(endpoint: str, **kwargs: object) -> NoTrustedReplyWebSocket:
        fake_websockets.connect_calls.append((endpoint, kwargs))
        websocket = NoTrustedReplyWebSocket()
        fake_websockets.connections.append(websocket)
        return websocket

    fake_websockets.connect = connect  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets)

    with pytest.raises(QQEvalDriverError) as raised:
        execute_qq_live_case(
            scenario="group_at_text",
            bot_id=BOT,
            whitelist_ids=[SENDER],
            confirm_external_write=True,
            env=_env(),
            prompt="请完成测评",
            evidence_key=KEY,
        )

    assert raised.value.failure_class == "infrastructure_error"
    assert raised.value.code == "qq_live_transport_unavailable"
