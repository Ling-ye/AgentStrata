"""Public-image download helpers for workspace tools."""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from chatcopilot.agent.tools.workspace_context import resolve_workspace
from chatcopilot.contracts.tools import ToolContext, ToolResult

_IMAGE_DEFAULT_LIMIT = 3
_IMAGE_MAX_LIMIT = 5
_IMAGE_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_HARD_MAX_BYTES = 20 * 1024 * 1024
_IMAGE_TIMEOUT_SECONDS = 15
_IMAGE_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_IMAGE_EXT_BY_KIND = {
    "jpeg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
}


def _handler_download_image_urls(
    args: Dict[str, Any], _ctx: ToolContext
) -> ToolResult:
    raw_urls = args.get("urls")
    if not isinstance(raw_urls, (list, tuple)) or not raw_urls:
        raise ValueError("缺少必填参数: urls (非空数组)")

    limit = int(args.get("limit") or _IMAGE_DEFAULT_LIMIT)
    if limit <= 0:
        raise ValueError("limit 必须为正整数")
    limit = min(limit, _IMAGE_MAX_LIMIT)

    max_bytes = int(args.get("max_bytes") or _IMAGE_DEFAULT_MAX_BYTES)
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须为正整数")
    max_bytes = min(max_bytes, _IMAGE_HARD_MAX_BYTES)

    ws = resolve_workspace(create=True)
    out_dir = ws.downloads / "images"
    out_dir.mkdir(parents=True, exist_ok=True)

    downloaded: List[Path] = []
    failed: List[str] = []
    seen: set[str] = set()
    for raw_url in raw_urls:
        if len(downloaded) >= limit:
            break
        url = str(raw_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            target = _download_one_image_url(out_dir, url, max_bytes=max_bytes)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{url}: {type(exc).__name__}: {exc}")
            continue
        if not ws.is_inside(target):
            raise PermissionError(f"下载目标越出工作区: {target}")
        downloaded.append(target)

    if not downloaded:
        details = "\n".join(failed[:5])
        raise RuntimeError(
            "没有成功下载任何图片。"
            + (f"\n失败明细:\n{details}" if details else "")
        )

    lines = [
        f"已下载 {len(downloaded)} 张图片到 downloads/images/，可继续调用 send_files_to_user 发送。",
        *[f"- {ws.relpath(path)} ({path.stat().st_size} bytes)" for path in downloaded],
    ]
    if failed:
        lines.append(f"另有 {len(failed)} 个 URL 下载失败。")
    return ToolResult(
        ok=True,
        summary="\n".join(lines),
        outputs=[str(path) for path in downloaded],
        data={
            "downloaded_count": len(downloaded),
            "failed_count": len(failed),
        },
        file_type_hint="image",
    )


def _download_one_image_url(out_dir: Path, url: str, *, max_bytes: int) -> Path:
    _validate_public_http_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AgentStrata/1.0",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=_IMAGE_TIMEOUT_SECONDS) as response:
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                announced = int(content_length)
            except ValueError:
                announced = 0
            if announced > max_bytes:
                raise ValueError(f"图片超过大小上限: {announced} > {max_bytes}")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"图片超过大小上限: {total} > {max_bytes}")
            chunks.append(chunk)

    data = b"".join(chunks)
    kind = _detect_image_kind(data)
    if kind is None:
        raise ValueError("响应内容不是受支持的图片格式")
    if (
        content_type
        and content_type not in _IMAGE_EXT_BY_MIME
        and not content_type.startswith("application/octet-stream")
    ):
        raise ValueError(f"响应 Content-Type 不是图片: {content_type}")

    digest = hashlib.sha256(url.encode("utf-8") + data[:4096]).hexdigest()[:16]
    target = out_dir / f"image_{int(time.time())}_{digest}{_IMAGE_EXT_BY_KIND[kind]}"
    target.write_bytes(data)
    return target


def _validate_public_http_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("只允许 http/https 图片 URL")
    if not parsed.hostname:
        raise ValueError("图片 URL 缺少 host")
    host = parsed.hostname.strip()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        _reject_private_address(ip)
        return
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("拒绝 localhost 图片 URL")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"无法解析图片 URL host: {host}") from exc
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_text = str(sockaddr[0])
        try:
            _reject_private_address(ipaddress.ip_address(ip_text))
        except ValueError as exc:
            raise ValueError(f"拒绝非公网图片地址: {ip_text}") from exc


def _reject_private_address(ip: ipaddress._BaseAddress) -> None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise ValueError(f"拒绝非公网图片地址: {ip}")


def _detect_image_kind(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


__all__ = [
    "_IMAGE_DEFAULT_LIMIT",
    "_IMAGE_DEFAULT_MAX_BYTES",
    "_handler_download_image_urls",
]
