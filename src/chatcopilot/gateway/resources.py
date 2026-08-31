"""Post-admission materialization for authenticated QQ CDN resource tickets."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import http.client
import ipaddress
import socket
import ssl
from typing import Protocol
from urllib.parse import urlsplit

from chatcopilot.application.resources import FetchedResource
from chatcopilot.contracts.gateway import ResourceTicket


_QQ_CDN_SUFFIXES = ("qpic.cn", "qq.com.cn")
_MAX_URL_CHARS = 4096


class GatewayResourceFetchError(RuntimeError):
    """Stable rejection that never includes a provider URL or host path."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class HttpsByteReaderPort(Protocol):
    async def read(
        self,
        url: str,
        *,
        host: str,
        addresses: tuple[str, ...],
        max_bytes: int,
    ) -> tuple[bytes, str | None]: ...


AddressResolver = Callable[[str, int], Iterable[str]]


class QqCdnResourceFetcher:
    """Fetch only bounded HTTPS resources named by authenticated QQ message frames."""

    def __init__(
        self,
        *,
        reader: HttpsByteReaderPort | None = None,
        resolver: AddressResolver | None = None,
        allowed_domain_suffixes: tuple[str, ...] = _QQ_CDN_SUFFIXES,
    ) -> None:
        suffixes = tuple(item.strip().lower().lstrip(".") for item in allowed_domain_suffixes)
        if not suffixes or any(not item or "." not in item for item in suffixes):
            raise ValueError("allowed_domain_suffixes must contain DNS suffixes")
        self._reader = reader or _PinnedHttpsByteReader()
        self._resolver = resolver or _resolve_addresses
        self._suffixes = suffixes

    async def fetch(self, ticket: ResourceTicket, *, max_bytes: int) -> FetchedResource:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if ticket.account.channel != "qq":
            raise GatewayResourceFetchError(
                "resource_channel_unsupported",
                "Resource ticket is not owned by the QQ Channel",
            )
        raw_url = ticket.provider_ref.get("url")
        if not isinstance(raw_url, str):
            raise GatewayResourceFetchError(
                "resource_provider_url_missing",
                "QQ provider did not supply a downloadable resource URL",
            )
        url, host = self._validate_url(raw_url)
        try:
            addresses = tuple(await asyncio.to_thread(self._resolver, host, 443))
        except Exception as exc:
            raise GatewayResourceFetchError(
                "resource_dns_unavailable",
                "QQ resource host could not be resolved",
            ) from exc
        if not addresses or any(not _is_public_address(item) for item in addresses):
            raise GatewayResourceFetchError(
                "resource_address_not_public",
                "QQ resource host did not resolve exclusively to public addresses",
            )
        try:
            data, response_type = await self._reader.read(
                url,
                host=host,
                addresses=addresses,
                max_bytes=max_bytes,
            )
        except GatewayResourceFetchError:
            raise
        except Exception as exc:
            raise GatewayResourceFetchError(
                "resource_download_failed",
                "QQ resource download failed",
            ) from exc
        if not isinstance(data, bytes) or len(data) > max_bytes:
            raise GatewayResourceFetchError(
                "resource_download_size_invalid",
                "QQ resource exceeded its authorized byte limit",
            )
        media_type = _media_type(response_type) or ticket.media_type
        return FetchedResource(data=data, name=ticket.name, media_type=media_type)

    def _validate_url(self, raw_url: str) -> tuple[str, str]:
        if not raw_url or len(raw_url) > _MAX_URL_CHARS or any(
            ord(character) < 32 for character in raw_url
        ):
            raise GatewayResourceFetchError(
                "resource_provider_url_invalid",
                "QQ provider resource URL is invalid",
            )
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except ValueError as exc:
            raise GatewayResourceFetchError(
                "resource_provider_url_invalid",
                "QQ provider resource URL is invalid",
            ) from exc
        host = str(parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or not parsed.path
            or parsed.fragment
            or not _matches_suffix(host, self._suffixes)
        ):
            raise GatewayResourceFetchError(
                "resource_provider_url_not_allowed",
                "QQ provider resource URL is outside the approved HTTPS boundary",
            )
        return raw_url, host


class _PinnedHttpsByteReader:
    async def read(
        self,
        url: str,
        *,
        host: str,
        addresses: tuple[str, ...],
        max_bytes: int,
    ) -> tuple[bytes, str | None]:
        return await asyncio.to_thread(
            self._read_sync,
            url,
            host=host,
            addresses=addresses,
            max_bytes=max_bytes,
        )

    @staticmethod
    def _read_sync(
        url: str,
        *,
        host: str,
        addresses: tuple[str, ...],
        max_bytes: int,
    ) -> tuple[bytes, str | None]:
        parsed = urlsplit(url)
        target = parsed.path + (("?" + parsed.query) if parsed.query else "")
        last_error: OSError | None = None
        for address in addresses:
            connection = _PinnedHttpsConnection(
                host=host,
                address=address,
                port=443,
                timeout=15.0,
            )
            try:
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Accept": "*/*",
                        "Connection": "close",
                        "Host": host,
                        "User-Agent": "AgentStrata-Gateway/1",
                    },
                )
                response = connection.getresponse()
                if response.status != 200:
                    raise GatewayResourceFetchError(
                        "resource_download_rejected",
                        "QQ resource server rejected the download",
                    )
                declared = response.getheader("Content-Length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError as exc:
                        raise GatewayResourceFetchError(
                            "resource_content_length_invalid",
                            "QQ resource server returned an invalid byte length",
                        ) from exc
                    if declared_size < 0 or declared_size > max_bytes:
                        raise GatewayResourceFetchError(
                            "resource_download_size_invalid",
                            "QQ resource exceeded its authorized byte limit",
                        )
                chunks: list[bytes] = []
                observed = 0
                while True:
                    chunk = response.read(min(64 * 1024, max_bytes - observed + 1))
                    if not chunk:
                        break
                    observed += len(chunk)
                    if observed > max_bytes:
                        raise GatewayResourceFetchError(
                            "resource_download_size_invalid",
                            "QQ resource exceeded its authorized byte limit",
                        )
                    chunks.append(chunk)
                return b"".join(chunks), response.getheader("Content-Type")
            except GatewayResourceFetchError:
                raise
            except OSError as exc:
                last_error = exc
            finally:
                connection.close()
        raise GatewayResourceFetchError(
            "resource_download_failed",
            "QQ resource download failed",
        ) from last_error


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    def __init__(self, *, host: str, address: str, port: int, timeout: float) -> None:
        tls_context = ssl.create_default_context()
        super().__init__(
            host=host,
            port=port,
            timeout=timeout,
            context=tls_context,
        )
        self._address = address
        self._tls_context = tls_context

    def connect(self) -> None:
        if not _is_public_address(self._address):
            raise OSError("pinned HTTPS address is not public")
        raw_socket = socket.create_connection(
            (self._address, self.port),
            self.timeout,
            None,
        )
        try:
            self.sock = self._tls_context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
            peer = str(self.sock.getpeername()[0])
            if (
                ipaddress.ip_address(peer) != ipaddress.ip_address(self._address)
                or not _is_public_address(peer)
            ):
                raise OSError("pinned HTTPS peer identity changed")
        except Exception:
            if self.sock is not None:
                self.sock.close()
            else:
                raw_socket.close()
            self.sock = None
            raise


def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(record[4][0]) for record in records))


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global


def _matches_suffix(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _media_type(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.split(";", 1)[0].strip().lower()
    if not candidate or len(candidate) > 127 or "/" not in candidate:
        return None
    if any(ord(character) < 33 or ord(character) > 126 for character in candidate):
        return None
    return candidate


__all__ = [
    "GatewayResourceFetchError",
    "HttpsByteReaderPort",
    "QqCdnResourceFetcher",
]
