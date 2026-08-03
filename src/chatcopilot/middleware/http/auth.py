"""HTTP authentication helpers."""
from __future__ import annotations

import hmac
import os
from http import HTTPStatus
from typing import Mapping

from chatcopilot.project import ENV_PREFIX


DEFAULT_API_TOKEN_ENV = f"{ENV_PREFIX}_HTTP_API_TOKEN"


class HttpError(RuntimeError):
    """Error with an HTTP status code and machine-readable message."""

    def __init__(self, status: HTTPStatus, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def require_bearer_token(headers: Mapping[str, str], *, token_env: str = DEFAULT_API_TOKEN_ENV) -> None:
    expected = os.environ.get(token_env, "").strip()
    if not expected:
        raise HttpError(HTTPStatus.INTERNAL_SERVER_ERROR, f"未配置 {token_env}")

    auth_header = headers.get("Authorization", "") or headers.get("authorization", "")
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        raise HttpError(HTTPStatus.UNAUTHORIZED, "缺少 Bearer Token")

    actual = auth_header[len(prefix):].strip()
    if not hmac.compare_digest(actual, expected):
        raise HttpError(HTTPStatus.FORBIDDEN, "Bearer Token 不匹配")
