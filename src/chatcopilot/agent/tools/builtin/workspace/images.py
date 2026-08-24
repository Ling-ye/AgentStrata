"""Public-image download and conversation-delivery helpers."""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence

from chatcopilot.agent.tools.file_delivery import (
    FileDeliveryResult,
    get_current_file_sender,
)
from chatcopilot.agent.tools.workspace_context import resolve_workspace
from chatcopilot.contracts.tools import ToolContext, ToolResult

_IMAGE_DEFAULT_LIMIT = 3
_IMAGE_MAX_LIMIT = 5
_IMAGE_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_HARD_MAX_BYTES = 20 * 1024 * 1024
_IMAGE_TIMEOUT_SECONDS = 15
_IMAGE_MAX_REDIRECTS = 5
_IMAGE_FAKE_IP_NETWORK = ipaddress.ip_network("198.18." + "0.0/15")
_IMAGE_DOH_HOST = "cloudflare-dns.com"
_IMAGE_DOH_BOOTSTRAP_ADDRESSES = ("1.1.1.1", "1.0.0.1")
_IMAGE_DOH_TIMEOUT_SECONDS = 5
_IMAGE_DOH_MAX_BYTES = 64 * 1024
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
_IMAGE_KIND_BY_MIME = {
    media_type: extension.removeprefix(".").replace("jpg", "jpeg")
    for media_type, extension in _IMAGE_EXT_BY_MIME.items()
}
_IMAGE_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_IMAGE_REQUEST_HEADERS = {
    "User-Agent": "AgentStrata/1.0",
    "Accept": "image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
    "Accept-Encoding": "identity",
}


@dataclass(frozen=True)
class _ResolvedPublicUrl:
    parsed: urllib.parse.SplitResult
    host: str
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class _ImageHttpResponse:
    status: int
    content_type: str = ""
    content_length: str = ""
    location: str = ""
    data: bytes = b""


@dataclass(frozen=True)
class _ImageDownloadBatch:
    paths: tuple[Path, ...]
    failures: tuple[str, ...]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str) -> None:
        super().__init__(host, port=port, timeout=_IMAGE_TIMEOUT_SECONDS)
        self._resolved_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._resolved_address, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str) -> None:
        super().__init__(
            host,
            port=port,
            timeout=_IMAGE_TIMEOUT_SECONDS,
            context=ssl.create_default_context(),
        )
        self._resolved_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._resolved_address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


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
    batch = _download_image_batch(raw_urls, workspace=ws, limit=limit, max_bytes=max_bytes)
    if not batch.paths:
        raise RuntimeError(_no_images_error(batch.failures))

    lines = [
        f"已下载 {len(batch.paths)} 张图片到 downloads/images/，可继续调用 send_files_to_user 发送。",
        *[f"- {ws.relpath(path)} ({path.stat().st_size} bytes)" for path in batch.paths],
    ]
    if batch.failures:
        lines.append(f"另有 {len(batch.failures)} 个 URL 下载失败。")
    return ToolResult(
        ok=True,
        summary="\n".join(lines),
        outputs=[str(path) for path in batch.paths],
        data={
            "downloaded_count": len(batch.paths),
            "failed_count": len(batch.failures),
        },
        file_type_hint="image",
    )


def _handler_send_image_urls_to_user(
    args: Dict[str, Any], _ctx: ToolContext
) -> ToolResult:
    raw_urls = args.get("urls")
    if not isinstance(raw_urls, (list, tuple)) or not raw_urls:
        raise ValueError("缺少必填参数: urls (非空数组)")
    if len(raw_urls) > _IMAGE_MAX_LIMIT:
        raise ValueError(f"urls 最多 {_IMAGE_MAX_LIMIT} 项")

    sender = get_current_file_sender()
    if sender is None:
        return ToolResult(
            ok=False,
            error="当前会话未注入文件回传通道，未下载或发送图片。",
            error_code="file_delivery_unavailable",
            stage="preflight",
        )

    message = str(args.get("message") or "").strip()
    ws = resolve_workspace(create=True)
    batch = _download_image_batch(
        raw_urls,
        workspace=ws,
        limit=_IMAGE_MAX_LIMIT,
        max_bytes=_IMAGE_DEFAULT_MAX_BYTES,
        candidate_limit=_IMAGE_MAX_LIMIT,
    )
    if not batch.paths:
        return ToolResult(
            ok=False,
            error=_no_images_error(batch.failures),
            error_code="image_download_failed",
            stage="download",
            details={"failed_count": len(batch.failures)},
        )

    try:
        delivery = sender([str(path) for path in batch.paths], message)
    except Exception:  # noqa: BLE001 - delivery may already have reached OneBot
        return ToolResult(
            ok=False,
            error="图片已下载，但平台发送结果未确认；不要自动重试或声称已经发送。",
            error_code="image_delivery_unconfirmed",
            stage="delivery",
            details={
                "downloaded_count": len(batch.paths),
                "failed_count": len(batch.failures),
                "delivery_status": "unknown",
            },
        )

    if not isinstance(delivery, FileDeliveryResult) or (
        len(delivery.sent_paths) != len(batch.paths)
        or len(delivery.sent_names) != len(batch.paths)
    ):
        return ToolResult(
            ok=False,
            error="平台返回的图片发送回执不完整；不要声称已经发送。",
            error_code="image_delivery_receipt_invalid",
            stage="delivery_receipt",
            details={
                "downloaded_count": len(batch.paths),
                "failed_count": len(batch.failures),
                "delivery_status": "unknown",
            },
        )

    summary = f"已发送 {len(delivery.sent_paths)} 张图片到当前会话：{', '.join(delivery.sent_names)}"
    if batch.failures:
        summary += f"；另有 {len(batch.failures)} 个 URL 下载失败。"
        summary += "\n" + "\n".join(batch.failures)
    return ToolResult(
        ok=True,
        summary=summary,
        outputs=list(delivery.sent_paths),
        data={
            "downloaded_count": len(batch.paths),
            "sent_count": len(delivery.sent_paths),
            "sent_names": list(delivery.sent_names),
            "failed_count": len(batch.failures),
        },
        file_type_hint="image",
    )


def _download_image_batch(
    raw_urls: Sequence[object],
    *,
    workspace: Any,
    limit: int,
    max_bytes: int,
    candidate_limit: int | None = None,
) -> _ImageDownloadBatch:
    out_dir = workspace.downloads / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    if out_dir.is_symlink() or not workspace.is_inside(out_dir):
        raise PermissionError("图片下载目录越出工作区")

    downloaded: List[Path] = []
    failed: List[str] = []
    seen: set[str] = set()
    candidates = raw_urls[:candidate_limit] if candidate_limit is not None else raw_urls
    for index, raw_url in enumerate(candidates, start=1):
        if len(downloaded) >= limit:
            break
        url = str(raw_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            target = _download_one_image_url(out_dir, url, max_bytes=max_bytes)
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not block valid peers
            failed.append(
                f"URL #{index} {_safe_url_label(url)}：{type(exc).__name__}: {exc}"
            )
            continue
        if not workspace.is_inside(target):
            raise PermissionError("下载目标越出工作区")
        downloaded.append(target)
    return _ImageDownloadBatch(tuple(downloaded), tuple(failed))


def _download_one_image_url(out_dir: Path, url: str, *, max_bytes: int) -> Path:
    data, content_type = _fetch_image_url(url, max_bytes=max_bytes)
    kind = _detect_image_kind(data)
    if kind is None:
        raise ValueError("响应内容不是受支持的图片格式")
    if content_type and content_type != "application/octet-stream":
        declared_kind = _IMAGE_KIND_BY_MIME.get(content_type)
        if declared_kind is None:
            raise ValueError(f"响应 Content-Type 不是图片: {content_type}")
        if declared_kind != kind:
            raise ValueError("响应 Content-Type 与图片签名不匹配")

    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix="image_",
        suffix=_IMAGE_EXT_BY_KIND[kind],
        dir=out_dir,
        delete=False,
    ) as handle:
        handle.write(data)
        return Path(handle.name)


def _fetch_image_url(url: str, *, max_bytes: int) -> tuple[bytes, str]:
    current_url = url
    visited: set[str] = set()
    for redirect_count in range(_IMAGE_MAX_REDIRECTS + 1):
        if current_url in visited:
            raise ValueError("图片 URL 重定向形成循环")
        visited.add(current_url)
        resolved = _resolve_public_url(current_url)
        response = _request_image_once(resolved, max_bytes=max_bytes)
        if response.status in _IMAGE_REDIRECT_STATUSES:
            if redirect_count >= _IMAGE_MAX_REDIRECTS:
                raise ValueError(f"图片 URL 重定向超过 {_IMAGE_MAX_REDIRECTS} 次")
            if not response.location:
                raise ValueError("图片 URL 重定向缺少 Location")
            current_url = urllib.parse.urljoin(current_url, response.location)
            continue
        if response.status < 200 or response.status >= 300:
            raise ValueError(f"图片请求返回 HTTP {response.status}")
        return response.data, response.content_type
    raise ValueError(f"图片 URL 重定向超过 {_IMAGE_MAX_REDIRECTS} 次")


def _resolve_public_url(url: str) -> _ResolvedPublicUrl:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("只允许 http/https 图片 URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("图片 URL 不允许携带用户信息")
    if not parsed.hostname:
        raise ValueError("图片 URL 缺少 host")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("图片 URL 端口无效") from exc

    raw_host = parsed.hostname.strip()
    try:
        literal_ip = ipaddress.ip_address(raw_host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        _require_public_address(literal_ip)
        host = raw_host
        addresses = (str(literal_ip),)
    else:
        host = raw_host.encode("idna").decode("ascii")
        if host.lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError("拒绝 localhost 图片 URL")
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"无法解析图片 URL host: {host}") from exc
        resolved_addresses: list[str] = []
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            ip_text = str(sockaddr[0]).split("%", 1)[0]
            try:
                address = ipaddress.ip_address(ip_text)
            except ValueError as exc:
                raise ValueError("图片 URL DNS 返回无效地址") from exc
            normalized = str(address)
            if normalized not in resolved_addresses:
                resolved_addresses.append(normalized)
        if not resolved_addresses:
            raise ValueError(f"图片 URL host 未解析到地址: {host}")
        if all(_is_fake_ip_address(address) for address in resolved_addresses):
            # Transparent proxies may synthesize RFC 2544 addresses. Resolve through a
            # pinned public DoH endpoint instead of connecting to the reserved address.
            resolved_addresses = list(_resolve_hostname_via_doh(host))
        for address in resolved_addresses:
            _require_public_address(ipaddress.ip_address(address))
        addresses = tuple(resolved_addresses)
    return _ResolvedPublicUrl(parsed, host, port, addresses)


def _is_fake_ip_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return isinstance(parsed, ipaddress.IPv4Address) and parsed in _IMAGE_FAKE_IP_NETWORK


def _resolve_hostname_via_doh(host: str) -> tuple[str, ...]:
    for record_name, record_type in (("A", 1), ("AAAA", 28)):
        document = _request_doh_document(host, record_name)
        if int(document.get("Status", -1)) != 0:
            raise ValueError("安全公网 DNS 查询未成功")
        questions = document.get("Question")
        if not isinstance(questions, list) or host.lower() not in {
            str(item.get("name") or "").rstrip(".").lower()
            for item in questions
            if isinstance(item, dict)
        }:
            raise ValueError("安全公网 DNS 回应与请求不匹配")
        resolved: list[str] = []
        answers = document.get("Answer")
        if not isinstance(answers, list):
            answers = []
        for answer in answers:
            if not isinstance(answer, dict) or answer.get("type") != record_type:
                continue
            try:
                address = ipaddress.ip_address(str(answer.get("data") or ""))
            except ValueError as exc:
                raise ValueError("安全公网 DNS 返回无效地址") from exc
            _require_public_address(address)
            normalized = str(address)
            if normalized not in resolved:
                resolved.append(normalized)
            if len(resolved) >= 16:
                break
        if resolved:
            return tuple(resolved)
    raise ValueError("安全公网 DNS 未返回可用地址")


def _request_doh_document(host: str, record_type: str) -> dict[str, Any]:
    path = "/dns-query?" + urllib.parse.urlencode(
        {"name": host, "type": record_type}
    )
    last_error: Exception | None = None
    for address in _IMAGE_DOH_BOOTSTRAP_ADDRESSES:
        connection = _PinnedHTTPSConnection(_IMAGE_DOH_HOST, 443, address)
        connection.timeout = _IMAGE_DOH_TIMEOUT_SECONDS
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "application/dns-json",
                    "User-Agent": _IMAGE_REQUEST_HEADERS["User-Agent"],
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"安全公网 DNS 返回 HTTP {response.status}")
            content_type = (
                str(response.getheader("Content-Type") or "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if content_type != "application/dns-json":
                raise ValueError("安全公网 DNS 返回了非 JSON 响应")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(16 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _IMAGE_DOH_MAX_BYTES:
                    raise ValueError("安全公网 DNS 响应超过大小上限")
                chunks.append(chunk)
            document = json.loads(b"".join(chunks))
            if not isinstance(document, dict):
                raise ValueError("安全公网 DNS 返回格式无效")
            return document
        except (OSError, http.client.HTTPException, ValueError) as exc:
            last_error = exc
        finally:
            connection.close()
    raise ValueError("无法连接安全公网 DNS") from last_error


def _request_image_once(
    resolved: _ResolvedPublicUrl,
    *,
    max_bytes: int,
) -> _ImageHttpResponse:
    last_error: Exception | None = None
    for address in resolved.addresses:
        connection: http.client.HTTPConnection
        if resolved.parsed.scheme.lower() == "https":
            connection = _PinnedHTTPSConnection(resolved.host, resolved.port, address)
        else:
            connection = _PinnedHTTPConnection(resolved.host, resolved.port, address)
        try:
            path = urllib.parse.urlunsplit(
                ("", "", resolved.parsed.path or "/", resolved.parsed.query, "")
            )
            connection.request("GET", path, headers=_IMAGE_REQUEST_HEADERS)
            response = connection.getresponse()
            content_type = str(response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            content_length = str(response.getheader("Content-Length") or "").strip()
            location = str(response.getheader("Location") or "").strip()
            if response.status in _IMAGE_REDIRECT_STATUSES:
                return _ImageHttpResponse(
                    status=response.status,
                    content_type=content_type,
                    content_length=content_length,
                    location=location,
                )
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
            return _ImageHttpResponse(
                status=response.status,
                content_type=content_type,
                content_length=content_length,
                data=b"".join(chunks),
            )
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()
    raise ValueError("无法连接图片地址") from last_error


def _require_public_address(ip: ipaddress._BaseAddress) -> None:
    if not ip.is_global:
        raise ValueError(f"拒绝非公网图片地址: {ip}")


def _safe_url_label(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or "<unknown-host>"
        port = parsed.port
        default_port = 443 if scheme == "https" else 80
        netloc = host if port in (None, default_port) else f"{host}:{port}"
        path = parsed.path or "/"
        return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


def _no_images_error(failures: Sequence[str]) -> str:
    details = "\n".join(failures[:_IMAGE_MAX_LIMIT])
    return "没有成功下载任何图片。" + (f"\n失败明细:\n{details}" if details else "")


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
    "_IMAGE_MAX_LIMIT",
    "_handler_download_image_urls",
    "_handler_send_image_urls_to_user",
]
