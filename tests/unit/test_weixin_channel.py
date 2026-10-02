"""Hermetic Weixin protocol and host-boundary regressions; no real Weixin login."""

import asyncio
import base64
from dataclasses import replace
from pathlib import Path

import pytest

from chatcopilot.authorization.policy import AdmissionPolicy, IdentityPolicy
from chatcopilot.botspec.channel_configuration import configuration_adapter
from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.deployment_env import deployment_environment, runtime_environment_keys
from chatcopilot.channels.base import (
    ChannelDefinitelyNotSubmittedError,
    ChannelDeliveryUnknownError,
)
from chatcopilot.channels.weixin_ilink.crypto import encrypt, decrypt
from chatcopilot.channels.weixin_ilink.codec import decode_message
from chatcopilot.channels.weixin_ilink.driver import WeixinDriver
from chatcopilot.channels.weixin_ilink.login import WeixinLoginService
from chatcopilot.channels.weixin_ilink.resources import WeixinResourceFetcher
from chatcopilot.channels.weixin_ilink.state import WeixinState
from chatcopilot.contracts.weixin import WeixinChannelConfig, WeixinError, media_key, https_url
from chatcopilot.contracts.authorization import AuthorizationRequest, AuthorizationOperation
from chatcopilot.contracts.gateway import (
    ChannelAccountRef,
    ConversationRef,
    MessageSegment,
    OutboundEnvelope,
)
from chatcopilot.contracts.identity import Identity, ConversationIdentity, TurnIdentity, Role


CONFIG = WeixinChannelConfig("bot-id", "weixin-owner", "private-token")
ROOT = Path(__file__).resolve().parents[2]


def message(**changes):
    return {
        "message_id": 12345678901234567890,
        "message_type": 1,
        "from_user_id": CONFIG.user_id,
        "to_user_id": CONFIG.account_id,
        "context_token": "private-context",
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        **changes,
    }


class Client:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {"ret": 0} if result is None else result
        self.error = error
        self.closed = False

    async def request(self, endpoint, body=None, **options):
        self.calls.append((endpoint, body))
        if self.error:
            raise self.error
        return self.result

    async def close(self):
        self.closed = True


def driver(tmp_path, client):
    state = WeixinState(tmp_path / "private", CONFIG.account_id)
    state.set_context(CONFIG.user_id, "context")

    async def accept(event):
        pass

    instance = WeixinDriver(CONFIG, accept, state=state, client=client)
    instance._status = "ready"
    instance._generation = "generation"
    return instance


def envelope(**changes):
    return OutboundEnvelope(
        "outbound-test",
        ChannelAccountRef("weixin", CONFIG.account_id),
        ConversationRef("p2p", CONFIG.user_id),
        (MessageSegment("text", text="reply"),),
        1,
        **changes,
    )


def test_codec_preserves_uint64_and_omits_reply_secrets():
    event = decode_message(message(), CONFIG, "generation", 1)
    assert event.evidence.message_id == "12345678901234567890"
    assert event.evidence.sender.sender_id == CONFIG.user_id
    assert "private-context" not in repr(event)
    assert "private-token" not in repr(CONFIG)
    changed_context = decode_message(
        message(context_token="new-context"), CONFIG, "new-generation", 2
    )
    assert event.evidence.frame_sha256 == changed_context.evidence.frame_sha256


@pytest.mark.parametrize(
    "change",
    [
        {"message_id": None},
        {"message_id": True},
        {"to_user_id": "another-bot"},
        {"item_list": [{"type": 1, "text_item": {"text": 123}}]},
    ],
)
def test_invalid_native_identity_is_not_normalized(change):
    with pytest.raises(WeixinError):
        decode_message(message(**change), CONFIG, "generation", 1)


def test_resources_remain_tickets_and_unsafe_names_are_rejected():
    media = {"encrypt_query_param": "private-url", "aes_key": base64.b64encode(b"1" * 16).decode()}
    file = {"type": 4, "file_item": {"file_name": "report.txt", "media": media}}
    event = decode_message(message(item_list=[file]), CONFIG, "generation", 1)
    assert event.resource_tickets[0].name == "report.txt"
    assert event.segments[0].resource_ticket_id == event.resource_tickets[0].ticket_id
    file["file_item"]["file_name"] = "../../secret"
    with pytest.raises(WeixinError, match="filename"):
        decode_message(message(item_list=[file]), CONFIG, "generation", 1)


@pytest.mark.parametrize(
    "sender,kind,account,allowed",
    [
        (CONFIG.user_id, "p2p", CONFIG.account_id, True),
        ("other", "p2p", CONFIG.account_id, False),
        (CONFIG.user_id, "group", CONFIG.account_id, False),
        (CONFIG.user_id, "p2p", "other-bot", False),
    ],
)
def test_gateway_admits_only_the_bound_private_user(sender, kind, account, allowed):
    turn = TurnIdentity(
        ConversationIdentity("weixin", kind, sender), sender, sender_user_name="Owner"
    )
    identity_policy = IdentityPolicy.from_iterables(owners=[Identity(name="Owner")])
    principal = identity_policy.principal(
        turn=turn, channel="weixin", account_id=account, evidence_digest="digest"
    )
    assert principal.role is Role.USER  # Display names never grant Weixin authority.
    policy = AdmissionPolicy.from_raw(
        qq_users=None,
        policy_version="v1",
        weixin_account=CONFIG.account_id,
        weixin_user=CONFIG.user_id,
    )
    request = AuthorizationRequest(
        "request", principal, AuthorizationOperation.INGRESS, "message", "digest"
    )
    assert policy.decide(request).allowed is allowed


def test_unconfigured_weixin_never_uses_non_qq_default_allow():
    turn = TurnIdentity(ConversationIdentity("weixin", "p2p", CONFIG.user_id), CONFIG.user_id)
    principal = IdentityPolicy().principal(
        turn=turn, channel="weixin", account_id=CONFIG.account_id, evidence_digest="digest"
    )
    policy = AdmissionPolicy.from_raw(qq_users=None, policy_version="v1")
    assert not policy.decide(
        AuthorizationRequest("r", principal, AuthorizationOperation.INGRESS, "m", "d")
    ).allowed


def test_crypto_is_strict_and_accepts_documented_key_encodings():
    key = bytes(range(16))
    data = b"media contents"
    assert decrypt(encrypt(data, key), key) == data
    assert media_key(base64.b64encode(key).decode()) == key
    assert media_key(base64.b64encode(key.hex().encode()).decode()) == key
    with pytest.raises(WeixinError):
        decrypt(b"invalid ciphertext", key)
    with pytest.raises(WeixinError):
        media_key("invalid-key")


@pytest.mark.parametrize(
    "url",
    [
        "http://ilinkai.weixin.qq.com",
        "https://evil.test/",
        "https://ilinkai.weixin.qq.com.evil.test/",
        "https://" + "fixture-user" + ":fixture-password" + "@" + "ilinkai.weixin.qq.com/",
        "https://ilinkai.weixin.qq.com:444/",
    ],
)
def test_https_boundary_rejects_untrusted_provider_urls(url):
    with pytest.raises(WeixinError):
        https_url(url)


def test_delivery_records_real_ack_without_fabricating_message_id(tmp_path):
    async def run():
        client = Client()
        instance = driver(tmp_path, client)
        receipt = await instance.send(envelope())
        assert receipt.stage == "provider_acknowledged" and receipt.provider_message_id is None
        assert client.calls[0][1]["msg"]["context_token"] == "context"
        assert len(client.calls) == 1

    asyncio.run(run())


def test_first_poll_messages_are_not_lost_and_shutdown_cancels_readers(tmp_path):
    async def run():
        delivered = asyncio.Event()

        class PollClient(Client):
            async def request(self, endpoint, body=None, **options):
                self.calls.append((endpoint, body))
                assert endpoint == "getupdates"
                if len(self.calls) == 1:
                    return {"ret": 0, "msgs": [message()], "get_updates_buf": "cursor-next"}
                await asyncio.Event().wait()

        client = PollClient()
        instance = driver(tmp_path, client)
        instance._status = "stopped"

        async def accept(event):
            delivered.set()

        instance.on_inbound = accept
        await instance.start()
        await asyncio.wait_for(delivered.wait(), 1)
        assert instance.state.cursor() == "cursor-next"
        assert instance.state.context(CONFIG.user_id) == "private-context"
        await instance.stop()
        assert instance.health().state == "stopped" and client.closed

    asyncio.run(run())


def test_json_streaming_is_bounded_and_bot_auth_never_goes_to_qr_status():
    async def run():
        from chatcopilot.channels.weixin_ilink.client import WeixinClient

        class Content:
            async def iter_chunked(self, size):
                yield b'{"ret":'
                yield b'0,"value":1}'

        class Response:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Http:
            calls = []

            def request(self, method, url, **kwargs):
                self.calls.append(kwargs)
                return Response()

        client = WeixinClient(token="fake-token")
        http = Http()
        client._session = http
        assert await client.request("sendmessage", {}) == {"ret": 0, "value": 1}
        assert http.calls[0]["headers"]["Authorization"] == "Bearer fake-token"
        await client.request("get_qrcode_status?qrcode=value", method="GET", authenticated=False)
        assert "Authorization" not in http.calls[1]["headers"]
        client.max_frame_bytes = 4
        with pytest.raises(WeixinError, match="too_large"):
            await client.request("sendmessage", {})

    asyncio.run(run())


@pytest.mark.parametrize(
    "code,kind",
    [
        ("weixin_network_error", ChannelDeliveryUnknownError),
        ("weixin_api_rejected", ChannelDefinitelyNotSubmittedError),
    ],
)
def test_delivery_error_classification_never_retries(tmp_path, code, kind):
    async def run():
        client = Client(error=WeixinError(code))
        instance = driver(tmp_path, client)
        with pytest.raises(kind):
            await instance.send(envelope())
        assert len(client.calls) == 1

    asyncio.run(run())


def test_missing_context_prevents_any_submission(tmp_path):
    async def run():
        client = Client()
        instance = driver(tmp_path, client)
        instance.state = WeixinState(tmp_path / "unbound", CONFIG.account_id)
        with pytest.raises(ChannelDefinitelyNotSubmittedError):
            await instance.send(envelope())
        assert client.calls == []

    asyncio.run(run())


def test_context_is_stored_only_after_durable_acceptance(tmp_path):
    async def run():
        instance = driver(tmp_path, Client())
        seen = []

        async def accept(event):
            seen.append(event)

        instance.on_inbound = accept
        await instance._accept(message())
        assert len(seen) == 1 and instance.state.context(CONFIG.user_id) == "private-context"

        class Denied(RuntimeError):
            code = "weixin-sender-not-allowed"

        async def deny(event):
            raise Denied()

        instance.on_inbound = deny
        await instance._accept(message(from_user_id="other", context_token="should-not-persist"))
        assert instance.state.context("other") is None

    asyncio.run(run())


def test_media_fetch_is_bounded_account_bound_and_decrypted(tmp_path):
    async def run():
        key = b"k" * 16

        class MediaClient(Client):
            async def download(self, url, *, max_bytes):
                assert max_bytes == 20
                return encrypt(b"data", key)

        event = decode_message(
            message(
                item_list=[
                    {
                        "type": 4,
                        "file_item": {
                            "file_name": "a.txt",
                            "media": {
                                "encrypt_query_param": "parameter",
                                "aes_key": base64.b64encode(key).decode(),
                            },
                        },
                    }
                ]
            ),
            CONFIG,
            "g",
            1,
        )
        fetcher = WeixinResourceFetcher(MediaClient(), account_id=CONFIG.account_id)
        resource = await fetcher.fetch(event.resource_tickets[0], max_bytes=4)
        assert resource.data == b"data"
        with pytest.raises(WeixinError):
            await WeixinResourceFetcher(MediaClient(), account_id="other").fetch(
                event.resource_tickets[0], max_bytes=4
            )

    asyncio.run(run())


class LoginClient(Client):
    def __init__(self, statuses):
        super().__init__()
        self.statuses = iter(statuses)
        self.base_url = "https://ilinkai.weixin.qq.com"

    async def request(self, endpoint, body=None, **options):
        self.calls.append((endpoint, body))
        if endpoint.startswith("get_bot_qrcode"):
            return {"qrcode": "private-qr", "qrcode_img_content": "https://example.test/scan"}
        return next(self.statuses)


def confirmed():
    return {
        "status": "confirmed",
        "bot_token": "private-token",
        "ilink_bot_id": CONFIG.account_id,
        "ilink_user_id": CONFIG.user_id,
        "baseurl": CONFIG.base_url,
    }


def test_login_publishes_only_after_confirmed_and_hides_credentials():
    async def run():
        client = LoginClient([confirmed()])
        service = WeixinLoginService(client_factory=lambda: client)
        saved = []
        initial = service.start("bot", saved.append)
        session = service.get("bot", initial["login_id"])
        await session.task
        assert session.status == "confirmed" and len(saved) == 1
        assert session.qr_image.startswith("data:image/svg+xml;base64,")
        assert "private-token" not in str(session.public()) and "private-qr" not in str(
            session.public()
        )
        assert client.closed

    asyncio.run(run())


def test_cancelled_login_ignores_a_late_provider_confirmation():
    async def run():
        entered = asyncio.Event()

        class Late(LoginClient):
            async def request(self, endpoint, body=None, **options):
                if endpoint.startswith("get_qrcode_status"):
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        return confirmed()
                return await super().request(endpoint, body, **options)

        client = Late([])
        saved = []
        service = WeixinLoginService(client_factory=lambda: client)
        initial = service.start("bot", saved.append)
        await entered.wait()
        result = await service.cancel("bot", initial["login_id"])
        assert result["status"] == "cancelled" and saved == [] and client.closed

    asyncio.run(run())


def test_save_failure_and_expired_login_are_not_reported_as_bound():
    async def run():
        def fail(binding):
            raise OSError("private failure")

        service = WeixinLoginService(client_factory=lambda: LoginClient([confirmed()]))
        result = service.start("bot", fail)
        session = service.get("bot", result["login_id"])
        await session.task
        assert session.status == "save_failed"
        service = WeixinLoginService(client_factory=lambda: LoginClient([{"status": "expired"}]))
        result = service.start("bot", fail)
        session = service.get("bot", result["login_id"])
        await session.task
        assert session.status == "expired"

    asyncio.run(run())


def test_independent_botspec_and_provisioning_do_not_require_legacy_adapter(tmp_path):
    spec = load_botspec(ROOT / "bots/lingye-copilot-weixin/bot.yaml")
    assert not [issue for issue in validate_botspec(spec) if issue.level == "error"]
    assert spec.platform.type == "weixin" and spec.channels.qq is None
    both = replace(
        spec,
        channels=replace(
            spec.channels, qq=load_botspec(ROOT / "bots/lingye-copilot-qq/bot.yaml").channels.qq
        ),
    )
    assert any(issue.level == "error" for issue in validate_botspec(both))
    adapter = configuration_adapter(spec)
    values = deployment_environment(spec, {}, source_root=ROOT, home=tmp_path)
    assert "CHATCOPILOT_CC_CONNECT_BIN" not in values
    assert "weixin" in values["CHATCOPILOT_WORKSPACE_ROOT"]
    assert all(
        key in runtime_environment_keys(spec, environment={})
        for key in ("WEIXIN_BOT_TOKEN", "WEIXIN_BOT_ID", "WEIXIN_USER_ID")
    )
    assert adapter.setup_actions() == ()
