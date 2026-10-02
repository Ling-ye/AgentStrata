"""Cancelable iLink HTTPS operations and bounded CDN transfers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import secrets
from urllib.parse import urlencode

import aiohttp
from aiohttp.resolver import DefaultResolver

from chatcopilot.contracts.weixin import API_BASE, CDN_BASE, WeixinError, https_url


from .crypto import encrypt


class _PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        answers = await super().resolve(host, port, family)
        if not answers or any(not ipaddress.ip_address(item["host"]).is_global for item in answers):
            raise WeixinError("weixin_address_not_public")
        return answers


class WeixinClient:
    def __init__(
        self, *, base_url: str = API_BASE, token: str = "", max_frame_bytes: int = 4 * 1024 * 1024
    ):
        self.base_url = https_url(base_url).rstrip("/")
        self.token = token
        self.max_frame_bytes = max_frame_bytes
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=_PublicResolver()), trust_env=False
            )
        return self._session

    async def request(
        self,
        endpoint: str,
        body: dict | None = None,
        *,
        timeout: float = 45,
        authenticated: bool = True,
        method: str = "POST",
    ) -> dict:
        url = https_url(self.base_url + "/ilink/bot/" + endpoint)
        headers = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": "256"}
        payload = dict(body or {})
        if method == "POST":
            headers.update(
                {
                    "AuthorizationType": "ilink_bot_token",
                    "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
                }
            )
            if authenticated:
                if not self.token:
                    raise WeixinError("weixin_not_bound")
                headers["Authorization"] = "Bearer " + self.token
                payload["base_info"] = {
                    "channel_version": "0.1.0",
                    "bot_agent": "AgentStrata/0.1.0",
                }
        try:
            async with self._http().request(
                method,
                url,
                json=payload if method == "POST" else None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise WeixinError("weixin_http_error")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > self.max_frame_bytes:
                        raise WeixinError("weixin_response_too_large")
                result = json.loads(raw)
            if not isinstance(result, dict):
                raise WeixinError("weixin_response_invalid")
            for name in ("ret", "errcode"):
                if name in result and (type(result[name]) is not int or result[name] != 0):
                    raise WeixinError(
                        "weixin_auth_expired" if result[name] == -14 else "weixin_api_rejected"
                    )
            return result
        except asyncio.TimeoutError:
            raise WeixinError(
                "weixin_poll_timeout" if endpoint == "getupdates" else "weixin_network_error"
            ) from None
        except aiohttp.ClientError:
            raise WeixinError("weixin_network_error") from None
        except (json.JSONDecodeError, UnicodeError):
            raise WeixinError("weixin_response_invalid") from None

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        try:
            async with self._http().get(
                https_url(url), timeout=aiohttp.ClientTimeout(total=60), allow_redirects=False
            ) as response:
                if response.status != 200:
                    raise WeixinError("weixin_download_failed")
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise WeixinError("weixin_resource_too_large")
                return bytes(data)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise WeixinError("weixin_download_failed") from None

    async def upload(self, data: bytes, *, user_id: str, kind: str, name: str) -> dict:
        key, filekey = secrets.token_bytes(16), secrets.token_hex(16)
        encrypted = encrypt(data, key)
        result = await self.request(
            "getuploadurl",
            {
                "filekey": filekey,
                "media_type": 1 if kind == "image" else 3,
                "to_user_id": user_id,
                "rawsize": len(data),
                "filesize": len(encrypted),
                "rawfilemd5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
                "aeskey": key.hex(),
                "no_need_thumb": True,
            },
        )
        url = result.get("upload_full_url")
        if not url:
            parameter = result.get("upload_param")
            if not isinstance(parameter, str) or not parameter:
                raise WeixinError("weixin_upload_parameters_missing")
            url = (
                CDN_BASE
                + "/upload?"
                + urlencode({"encrypted_query_param": parameter, "filekey": filekey})
            )
        try:
            async with self._http().post(
                https_url(url),
                data=encrypted,
                headers={"Content-Type": "application/octet-stream"},
                timeout=aiohttp.ClientTimeout(total=60),
                allow_redirects=False,
            ) as response:
                parameter = response.headers.get("x-encrypted-param")
                if response.status != 200 or not parameter:
                    raise WeixinError("weixin_upload_failed")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise WeixinError("weixin_upload_failed") from None
        media = {
            "encrypt_query_param": parameter,
            "aes_key": base64.b64encode(key.hex().encode()).decode(),
            "encrypt_type": 1,
        }
        if kind == "image":
            return {"type": 2, "image_item": {"media": media, "mid_size": len(encrypted)}}
        return {"type": 4, "file_item": {"media": media, "file_name": name, "len": str(len(data))}}
