"""GitHub catalog/asset transport; credentials never accompany an asset redirect."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import logging
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit

import httpx

from .character_types import catalog
from .repository import private_directory
from .types import GenerationError

MAX_CATALOG_BYTES = 2 * 1024 * 1024


@contextmanager
def private_transport_logs():
    # httpx INFO includes signed redirect URLs; httpcore DEBUG includes Location headers.
    # Silence only this request thread, leaving other requests' logging policy unchanged.
    owner = threading.get_ident()
    class PrivateRequest(logging.Filter):
        def filter(self, record):
            return record.thread != owner
    guard = PrivateRequest()
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore.connection", "httpcore.http11",
               "httpcore.http2", "httpcore.proxy", "httpcore.socks")]
    for logger in loggers:
        logger.addFilter(guard)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(guard)


class GitHubPresets:
    def __init__(self, repository: str, token: str, *, transport=None):
        self.repository, self._token, self._transport = repository, token, transport

    def _stream(self, path: str, *, accept: str, limit: int, sink):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise GenerationError("预设仓库须配置为 owner/repository")
        if not self._token:
            raise GenerationError("私有预设需要配置 AGENTSTRATA_LIVE2D_PRESET_TOKEN")
        url = f"https://api.github.com/repos/{self.repository}/{path}"
        headers = {"Accept": accept, "Authorization": "Bearer " + self._token,
                   "X-GitHub-Api-Version": "2022-11-28"}
        try:
            with private_transport_logs(), httpx.Client(timeout=60, follow_redirects=False, transport=self._transport) as client:
                for _ in range(4):
                    with client.stream("GET", url, headers=headers) as response:
                        if response.status_code in {401, 403, 404}:
                            raise GenerationError("无法访问私有预设：检查仓库、token 权限或 API 限流")
                        if response.status_code in {301, 302, 303, 307, 308}:
                            redirected = urlsplit(response.headers.get("location", ""))
                            if (redirected.scheme != "https" or redirected.hostname not in {
                                    "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
                                    or redirected.username or redirected.password or redirected.port not in {None, 443}):
                                raise GenerationError("预设下载重定向来源无效")
                            url, headers = redirected.geturl(), {"Accept": "application/octet-stream"}
                            continue
                        if response.status_code != 200:
                            raise GenerationError("预设服务暂时不可用，请稍后重试")
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > limit:
                                raise GenerationError("预设响应超过声明体积")
                            sink(chunk)
                        return total
                raise GenerationError("预设下载重定向次数超限")
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, GenerationError):
                raise
            raise GenerationError("预设下载失败；本地已保存角色不受影响") from None

    def catalog(self) -> dict:
        body = bytearray()
        self._stream("contents/catalog.json?ref=main", accept="application/vnd.github.raw+json",
                     limit=MAX_CATALOG_BYTES, sink=body.extend)
        try:
            return catalog(json.loads(body))
        except (ValueError, TypeError):
            raise GenerationError("远端预设目录格式无效") from None

    def download(self, asset_id: int, destination: Path, *, size: int, sha256: str) -> None:
        if destination.exists() or destination.is_symlink():
            raise GenerationError("下载目标已存在")
        private_directory(destination.parent)
        fd, name = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
        temporary = Path(name)
        checksum = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as stream:
                def sink(chunk):
                    checksum.update(chunk)
                    stream.write(chunk)
                count = self._stream(f"releases/assets/{asset_id}", accept="application/octet-stream",
                                     limit=size, sink=sink)
            if count != size or checksum.hexdigest() != sha256:
                raise GenerationError("预设下载大小或摘要不一致")
            temporary.rename(destination)
        finally:
            temporary.unlink(missing_ok=True)
