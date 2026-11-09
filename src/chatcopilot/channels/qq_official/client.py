"""Bounded HTTPS calls to fixed official endpoints, with cached access tokens."""
import asyncio
import time

import requests

from .config import QQOfficialError

API_BASE = "https://api.bot.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"


class QQOfficialClient:
    def __init__(self, config):
        self.config = config
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    def _request(self, method, url, body=None, headers=None):
        # Do not inherit proxies or .netrc credentials, follow redirects, or log provider bodies.
        with requests.Session() as session:
            session.trust_env = False
            try:
                with session.request(method, url, json=body, headers=headers,
                                     timeout=(5, self.config.action_timeout_seconds),
                                     allow_redirects=False, stream=True) as response:
                    if response.status_code == 401:
                        raise QQOfficialError("qq_official_auth_expired")
                    if not 200 <= response.status_code < 300:
                        raise QQOfficialError("qq_official_http_rejected" if response.status_code in {400, 401, 403, 404, 405, 413, 415, 422, 429} else "qq_official_transport_unknown")
                    deadline = time.monotonic() + self.config.action_timeout_seconds
                    raw = bytearray()
                    for chunk in response.iter_content(16384):
                        raw.extend(chunk)
                        if len(raw) > 1024 * 1024 or time.monotonic() > deadline:
                            raise QQOfficialError("qq_official_response_invalid")
                    import json
                    result = json.loads(raw)
            except QQOfficialError:
                raise
            except Exception:
                raise QQOfficialError("qq_official_transport_unknown") from None
        if not isinstance(result, dict):
            raise QQOfficialError("qq_official_response_invalid")
        if "code" in result and result["code"] not in (0, "0"):
            raise QQOfficialError("qq_official_api_rejected")
        return result

    async def token(self):
        async with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            result = await asyncio.to_thread(self._request, "POST", TOKEN_URL, {
                "appId": self.config.app_id, "clientSecret": self.config.app_secret,
            })
            token = result.get("access_token")
            try:
                ttl = int(result["expires_in"])
            except (KeyError, ValueError, TypeError):
                raise QQOfficialError("qq_official_token_invalid") from None
            if not isinstance(token, str) or not token or ttl <= 0:
                raise QQOfficialError("qq_official_token_invalid")
            self._token, self._expires = token, time.monotonic() + max(1, ttl - 60)
            return token

    async def request(self, method, path, body=None):
        token = await self.token()
        try:
            return await asyncio.to_thread(self._request, method, API_BASE + path, body, {
                "Authorization": "QQBot " + token, "X-Union-Appid": self.config.app_id,
            })
        except QQOfficialError as error:
            if error.code == "qq_official_auth_expired":
                self._token, self._expires = "", 0.0
            raise

    async def close(self):
        self._token, self._expires = "", 0.0




