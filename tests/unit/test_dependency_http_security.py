"""Regressions for untrusted HTTP inputs handled by the supported clients."""

from __future__ import annotations

import asyncio
from http.client import HTTPResponse as StdlibHTTPResponse
from http.cookies import SimpleCookie
import io

import aiohttp
import pytest
from urllib3.exceptions import ProtocolError
from urllib3.response import HTTPResponse
from yarl import URL


def test_remote_cookie_with_extreme_max_age_does_not_abort_response_processing():
    async def receive_cookie():
        jar = aiohttp.CookieJar()
        cookie = SimpleCookie()
        cookie["session"] = "synthetic"
        cookie["session"]["max-age"] = "9" * 1000
        origin = URL("https://example.com/")

        jar.update_cookies(cookie, origin)

        assert jar.filter_cookies(origin)["session"].value == "synthetic"

    asyncio.run(receive_cookie())


def test_chunk_extensions_are_rejected_before_an_unbounded_line_is_consumed():
    class ObservedStream(io.BytesIO):
        largest_line = 0

        def readline(self, size=-1):
            line = super().readline(size)
            self.largest_line = max(self.largest_line, len(line))
            return line

    class Socket:
        def __init__(self):
            self.stream = ObservedStream(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                + b"1;" + b"x" * (128 * 1024) + b"\r\nx\r\n0\r\n\r\n"
            )

        def makefile(self, mode):
            assert mode == "rb"
            return self.stream

    socket = Socket()
    original = StdlibHTTPResponse(socket, method="GET")
    original.begin()
    response = HTTPResponse(
        body=original,
        headers=dict(original.headers.items()),
        status=original.status,
        original_response=original,
        preload_content=False,
    )
    try:
        with pytest.raises(ProtocolError):
            list(response.read_chunked())
        # One extra byte is sufficient to detect an oversized line.
        assert socket.stream.largest_line <= 64 * 1024 + 1
    finally:
        response.close()
