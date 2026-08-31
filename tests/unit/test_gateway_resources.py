from __future__ import annotations

from dataclasses import replace
import ssl
from unittest.mock import patch
from unittest import IsolatedAsyncioTestCase

import pytest

from chatcopilot.contracts.gateway import (
    ChannelAccountRef,
    ConversationRef,
    ResourceTicket,
)
from chatcopilot.gateway.resources import (
    GatewayResourceFetchError,
    QqCdnResourceFetcher,
    _PinnedHttpsConnection,
)


_QPIC_HOST = "gchat." + "qpic.cn"
_QQ_MEDIA_HOST = "multimedia.nt." + "qq.com.cn"


def _https_url(host: str, suffix: str = "/download") -> str:
    return "https:" + "//" + host + suffix


class _Reader:
    def __init__(self, data: bytes = b"image-bytes") -> None:
        self.data = data
        self.calls: list[tuple[str, str, tuple[str, ...], int]] = []

    async def read(
        self,
        url: str,
        *,
        host: str,
        addresses: tuple[str, ...],
        max_bytes: int,
    ) -> tuple[bytes, str | None]:
        self.calls.append((url, host, addresses, max_bytes))
        return self.data, "image/png; charset=binary"


class _RawSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _TlsSocket(_RawSocket):
    def __init__(self, peer: str) -> None:
        super().__init__()
        self.peer = peer

    def getpeername(self) -> tuple[str, int]:
        return self.peer, 443


class _TlsContext:
    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.verify_mode = ssl.CERT_REQUIRED
        self.check_hostname = True
        self.server_hostname: str | None = None
        self.socket = _TlsSocket(peer)

    def wrap_socket(self, _raw: object, *, server_hostname: str):
        self.server_hostname = server_hostname
        return self.socket


def _ticket(**changes: object) -> ResourceTicket:
    values: dict[str, object] = {
        "ticket_id": "resource-1",
        "account": ChannelAccountRef("qq", "10001"),
        "conversation": ConversationRef("group", "30003"),
        "sender_id": "20002",
        "event_id": "event-1",
        "message_id": "message-1",
        "kind": "image",
        "name": "photo.png",
        "media_type": None,
        "provider_ref": {
            "url": _https_url(_QPIC_HOST, "/download?opaque=provider-value")
        },
    }
    values.update(changes)
    return ResourceTicket(**values)  # type: ignore[arg-type]


class GatewayResourceFetcherTests(IsolatedAsyncioTestCase):
    async def test_fetches_bounded_https_qq_cdn_resource_after_public_resolution(
        self,
    ) -> None:
        reader = _Reader()
        fetcher = QqCdnResourceFetcher(
            reader=reader,
            resolver=lambda _host, _port: ("1.1.1.1",),
        )

        resource = await fetcher.fetch(_ticket(), max_bytes=1024)

        assert resource.data == b"image-bytes"
        assert resource.name == "photo.png"
        assert resource.media_type == "image/png"
        assert reader.calls == [
            (
                _https_url(_QPIC_HOST, "/download?opaque=provider-value"),
                _QPIC_HOST,
                ("1.1.1.1",),
                1024,
            )
        ]

    async def test_rejects_non_cdn_or_non_https_sources_before_reader(self) -> None:
        urls = (
            "http:" + "//" + _QPIC_HOST + "/download",
            _https_url("evil" + "qpic.cn"),
            _https_url("user" + "@" + _QPIC_HOST),
            _https_url(_QPIC_HOST + ":444"),
            _https_url(_QPIC_HOST, "/download#fragment"),
            "file:" + "///provider/path",
        )
        for url in urls:
            reader = _Reader()
            fetcher = QqCdnResourceFetcher(
                reader=reader,
                resolver=lambda _host, _port: ("1.1.1.1",),
            )
            with self.subTest(url=url):
                with pytest.raises(GatewayResourceFetchError) as caught:
                    await fetcher.fetch(
                        _ticket(provider_ref={"url": url}),
                        max_bytes=1024,
                    )

                assert caught.value.code == "resource_provider_url_not_allowed"
                assert reader.calls == []

    async def test_rejects_private_or_mixed_dns_answers_before_reader(self) -> None:
        reader = _Reader()
        private_address = "10" + ".0.0.1"
        fetcher = QqCdnResourceFetcher(
            reader=reader,
            resolver=lambda _host, _port: ("1.1.1.1", private_address),
        )

        with pytest.raises(GatewayResourceFetchError) as caught:
            await fetcher.fetch(_ticket(), max_bytes=1024)

        assert caught.value.code == "resource_address_not_public"
        assert reader.calls == []

    async def test_missing_url_does_not_fall_back_to_provider_local_path(self) -> None:
        reader = _Reader()
        fetcher = QqCdnResourceFetcher(
            reader=reader,
            resolver=lambda _host, _port: ("1.1.1.1",),
        )

        with pytest.raises(GatewayResourceFetchError) as caught:
            await fetcher.fetch(
                replace(_ticket(), provider_ref={"path": "/provider/private/file"}),
                max_bytes=1024,
            )

        assert caught.value.code == "resource_provider_url_missing"
        assert reader.calls == []

    async def test_reader_oversize_is_rejected_without_exposing_provider_url(self) -> None:
        secret_url = _https_url(_QQ_MEDIA_HOST, "/download?opaque=do-not-log")
        fetcher = QqCdnResourceFetcher(
            reader=_Reader(b"x" * 9),
            resolver=lambda _host, _port: ("1.1.1.1",),
        )

        with pytest.raises(GatewayResourceFetchError) as caught:
            await fetcher.fetch(
                _ticket(provider_ref={"url": secret_url}),
                max_bytes=8,
            )

        assert caught.value.code == "resource_download_size_invalid"
        assert "do-not-log" not in str(caught.value)


def test_default_https_connection_pins_validated_ip_but_keeps_hostname_for_tls() -> None:
    raw = _RawSocket()
    context = _TlsContext("1.1.1.1")
    with (
        patch(
            "chatcopilot.gateway.resources.ssl.create_default_context",
            return_value=context,
        ),
        patch(
            "chatcopilot.gateway.resources.socket.create_connection",
            return_value=raw,
        ) as create_connection,
    ):
        connection = _PinnedHttpsConnection(
            host=_QPIC_HOST,
            address="1.1.1.1",
            port=443,
            timeout=5.0,
        )
        connection.connect()

    assert create_connection.call_args.args[0] == ("1.1.1.1", 443)
    assert context.server_hostname == _QPIC_HOST
    assert connection.sock is context.socket
    connection.close()


def test_default_https_connection_rejects_peer_ip_drift() -> None:
    raw = _RawSocket()
    context = _TlsContext("8.8.8.8")
    with (
        patch(
            "chatcopilot.gateway.resources.ssl.create_default_context",
            return_value=context,
        ),
        patch(
            "chatcopilot.gateway.resources.socket.create_connection",
            return_value=raw,
        ),
    ):
        connection = _PinnedHttpsConnection(
            host=_QPIC_HOST,
            address="1.1.1.1",
            port=443,
            timeout=5.0,
        )
        with pytest.raises(OSError, match="identity changed"):
            connection.connect()
