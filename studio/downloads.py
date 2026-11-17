"""Transmit only bytes from the checked build, including resumed downloads."""
import hashlib
import os
import secrets
import stat

import anyio
from starlette.datastructures import MutableHeaders
from starlette.responses import FileResponse


class VerifiedBuildResponse(FileResponse):
    """Keep one file open and check each transmitted chunk against its verified hash.

    FileResponse still owns HTTP range parsing and headers. Its sendfile/pathsend
    optimization must not reopen the mutable build path or bypass these checks.
    Only chunk digests are retained; no installer copy or large buffer is created.
    """

    def __init__(self, path, expected_digest, *, filename):
        self._file = None
        try:
            def opener(name, flags):
                return os.open(name, flags | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))

            self._file = open(path, "rb", opener=opener)
            info = os.fstat(self._file.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("安装包不是普通文件")
            self._size = info.st_size
            self._chunks = []
            digest = hashlib.sha256()
            remaining = self._size
            while remaining:
                length = min(self.chunk_size, remaining)
                chunk = self._file.read(length)
                if len(chunk) != length:
                    raise ValueError("安装包在校验时发生变化")
                digest.update(chunk)
                self._chunks.append(hashlib.sha256(chunk).digest())
                remaining -= len(chunk)
            if self._file.read(1) or digest.hexdigest() != expected_digest:
                raise ValueError("安装包摘要不一致")
            super().__init__(path, media_type="application/octet-stream", filename=filename,
                             stat_result=info, headers={"ETag": '"' + expected_digest + '"'})
        except BaseException:
            self.close()
            raise

    def close(self):
        if getattr(self, "_file", None) is not None:
            self._file.close()
            self._file = None

    def __del__(self):
        self.close()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Runs for invalid ranges, disconnects, cancellation and body errors.
            self.close()

    def _read_verified(self, start, end):
        index, offset = divmod(start, self.chunk_size)
        self._file.seek(index * self.chunk_size)
        length = min(self.chunk_size, self._size - index * self.chunk_size)
        chunk = self._file.read(length)
        if len(chunk) != length or hashlib.sha256(chunk).digest() != self._chunks[index]:
            raise ValueError("安装包在下载时发生变化，请重新核对构建")
        return chunk[offset:offset + min(end - start, length - offset)]

    async def _body(self, send, start, end, *, final=True):
        if start == end and final:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        while start < end:
            chunk = await anyio.to_thread.run_sync(self._read_verified, start, end)
            start += len(chunk)
            await send({"type": "http.response.body", "body": chunk, "more_body": start < end or not final})

    async def _handle_simple(self, send, send_header_only, send_pathsend):
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await self._body(send, 0, self._size)

    async def _handle_single_range(self, send, start, end, file_size, send_header_only):
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await self._body(send, start, end)

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only):
        boundary = secrets.token_hex(13)
        length, header = self.generate_multipart(ranges, boundary, file_size, self.headers["content-type"])
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = "multipart/byteranges; boundary=" + boundary
        headers["content-length"] = str(length)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            for start, end in ranges:
                await send({"type": "http.response.body", "body": header(start, end), "more_body": True})
                await self._body(send, start, end, final=False)
                await send({"type": "http.response.body", "body": b"\r\n", "more_body": True})
            await send({"type": "http.response.body", "body": f"--{boundary}--".encode("ascii"), "more_body": False})
