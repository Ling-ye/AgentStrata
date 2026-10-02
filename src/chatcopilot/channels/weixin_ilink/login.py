"""Finite QR binding sessions; credential publication is supplied by the host."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from io import BytesIO
import time
from typing import Callable
from urllib.parse import urlencode
import uuid

import qrcode
from qrcode.image.svg import SvgPathImage

from chatcopilot.contracts.weixin import WeixinApiClientPort
from chatcopilot.contracts.weixin import API_BASE, WeixinError, https_url, identity


TERMINAL = frozenset({"confirmed", "expired", "cancelled", "failed", "save_failed"})


@dataclass
class LoginSession:
    instance_id: str
    login_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "creating"
    qr_image: str | None = None
    error_code: str | None = None
    expires_at: float = field(default_factory=lambda: time.time() + 300)
    deadline: float = field(default_factory=lambda: time.monotonic() + 300, repr=False)
    verify_code: str | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "login_id": self.login_id,
            "status": self.status,
            "qr_image": self.qr_image,
            "error_code": self.error_code,
            "expires_at": self.expires_at,
        }


class WeixinLoginService:
    def __init__(self, *, client_factory: Callable[[], WeixinApiClientPort]):
        self._sessions: dict[str, LoginSession] = {}
        self._client_factory = client_factory

    def active(self, instance_id: str) -> bool:
        session = self._sessions.get(instance_id)
        return session is not None and session.status not in TERMINAL

    def get(self, instance_id: str, login_id: str) -> LoginSession:
        session = self._sessions.get(instance_id)
        if session is None or session.login_id != login_id:
            raise WeixinError("weixin_login_not_found")
        return session

    def start(
        self,
        instance_id: str,
        save: Callable[[dict], None],
        *,
        finish: Callable[[], None] = lambda: None,
        local_tokens: tuple[str, ...] = (),
    ) -> dict:
        if self.active(instance_id):
            raise WeixinError("weixin_login_active")
        session = LoginSession(instance_id)
        self._sessions[instance_id] = session
        session.task = asyncio.create_task(
            self._run(session, save, local_tokens), name="weixin-qr-binding"
        )

        def completed(task):
            if not task.cancelled():
                if task.exception() is not None:
                    session.status, session.error_code = "failed", "weixin_login_failed"
            finish()

        session.task.add_done_callback(completed)
        return session.public()

    def verify(self, instance_id: str, login_id: str, code: str) -> dict:
        session = self.get(instance_id, login_id)
        if (
            session.status != "need_verifycode"
            or not isinstance(code, str)
            or not code.isascii()
            or not code.isdigit()
            or not 1 <= len(code) <= 32
        ):
            raise WeixinError("weixin_verification_invalid")
        session.verify_code = code
        return session.public()

    async def cancel(self, instance_id: str, login_id: str) -> dict:
        session = self.get(instance_id, login_id)
        if session.status not in TERMINAL:
            session.status = "cancelled"
            if session.task is not None:
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)
        return session.public()

    async def close(self) -> None:
        for instance, session in tuple(self._sessions.items()):
            await self.cancel(instance, session.login_id)

    async def _run(self, session: LoginSession, save, local_tokens) -> None:
        client = self._client_factory()
        try:
            qr = await client.request(
                "get_bot_qrcode?bot_type=3",
                {"local_token_list": list(local_tokens)},
                authenticated=False,
            )
            if session.status in TERMINAL or self._sessions.get(session.instance_id) is not session:
                return
            qr_value, qr_content = qr.get("qrcode"), qr.get("qrcode_img_content")
            if (
                not isinstance(qr_value, str)
                or not isinstance(qr_content, str)
                or not qr_value
                or not qr_content
                or len(qr_content) > 4096
            ):
                raise WeixinError("weixin_qrcode_invalid")
            output = BytesIO()
            qrcode.make(qr_content, image_factory=SvgPathImage).save(output)
            session.qr_image = (
                "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode()
            )
            session.status = "wait"
            while time.monotonic() < session.deadline:
                if session.status == "need_verifycode" and session.verify_code is None:
                    await asyncio.sleep(0.2)
                    continue
                query = {"qrcode": qr_value}
                if session.verify_code is not None:
                    query["verify_code"] = session.verify_code
                    session.verify_code = None
                result = await client.request(
                    "get_qrcode_status?" + urlencode(query),
                    authenticated=False,
                    method="GET",
                    timeout=40,
                )
                if (
                    session.status in TERMINAL
                    or self._sessions.get(session.instance_id) is not session
                ):
                    return
                if time.monotonic() >= session.deadline:
                    session.status = "expired"
                    return
                status = result.get("status")
                if status == "confirmed":
                    identity(result.get("ilink_bot_id"))
                    identity(result.get("ilink_user_id"))
                    token = result.get("bot_token")
                    if not isinstance(token, str) or not token:
                        raise WeixinError("weixin_login_credentials_missing")
                    result["baseurl"] = https_url(result.get("baseurl") or API_BASE)
                    if (
                        self._sessions.get(session.instance_id) is not session
                        or session.status in TERMINAL
                    ):
                        return
                    try:
                        save(result)
                    except Exception as error:
                        session.status, session.error_code = (
                            "save_failed",
                            error.code
                            if isinstance(error, WeixinError)
                            else "weixin_credentials_save_failed",
                        )
                        return
                    session.status = "confirmed"
                    return
                if status == "scaned_but_redirect":
                    client.base_url = https_url(
                        "https://" + str(result.get("redirect_host", ""))
                    ).rstrip("/")
                    session.status = "scaned"
                elif status in {"wait", "scaned", "need_verifycode"}:
                    session.status = status
                elif status == "expired":
                    session.status = "expired"
                    return
                else:
                    raise WeixinError("weixin_login_status_invalid")
                await asyncio.sleep(0.5)
            session.status = "expired"
        except asyncio.CancelledError:
            session.status = "cancelled"
            raise
        except Exception as error:
            session.status = "failed"
            session.error_code = (
                error.code if isinstance(error, WeixinError) else "weixin_login_failed"
            )
        finally:
            session.verify_code = None
            await client.close()
