"""QQ OneBot configuration validation and authenticated health probes."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from urllib.parse import urlsplit


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class QQBoundaryError(ValueError):
    """Stable diagnostic that never includes the rejected secret value."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def require_access_token(token: str | None) -> str:
    value = str(token or "").strip()
    if not value:
        raise QQBoundaryError(
            "qq_access_token_missing",
            "QQ_ACCESS_TOKEN is required",
        )
    if _TOKEN_RE.fullmatch(value) is None:
        raise QQBoundaryError(
            "qq_access_token_invalid",
            "QQ_ACCESS_TOKEN must be 32-128 URL-safe characters",
        )
    return value


def require_loopback_websocket_url(
    value: str | None,
    *,
    env_key: str,
) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise QQBoundaryError(
            "qq_websocket_url_invalid",
            f"{env_key} must be a valid loopback WebSocket URL",
        ) from exc
    if (
        parsed.scheme not in {"ws", "wss"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is None
    ):
        raise QQBoundaryError(
            "qq_websocket_url_not_loopback",
            f"{env_key} must use ws/wss on localhost, 127.0.0.1, or ::1 with an explicit port",
        )
    return url


async def _connect_once(
    url: str,
    token: str | None,
) -> None:
    import websockets

    headers = {"Authorization": f"Bearer {token}"} if token else None
    connection_options = {
        "open_timeout": 3,
        "close_timeout": 1,
        "max_size": None,
    }
    kwargs = (
        {"additional_headers": headers, **connection_options}
        if headers
        else connection_options
    )
    try:
        try:
            connection = await websockets.connect(url, **kwargs)
        except TypeError:
            legacy_kwargs = (
                {"extra_headers": headers, **connection_options}
                if headers
                else connection_options
            )
            connection = await websockets.connect(url, **legacy_kwargs)
        echo = "chatcopilot-onebot-auth-probe"
        await connection.send(
            json.dumps(
                {"action": "get_status", "params": {}, "echo": echo},
                separators=(",", ":"),
            )
        )
        for _ in range(8):
            raw = await asyncio.wait_for(connection.recv(), timeout=3)
            response = json.loads(raw)
            try:
                retcode = int(response.get("retcode"))
            except (TypeError, ValueError):
                retcode = -1
            if (
                response.get("status") == "failed"
                and retcode == 1403
            ):
                raise PermissionError("OneBot rejected the access token")
            if response.get("echo") != echo:
                continue
            if response.get("status") != "ok" or retcode != 0:
                raise RuntimeError("OneBot probe action was rejected")
            return
        raise RuntimeError("OneBot probe response did not match the request")
    finally:
        connection_value = locals().get("connection")
        if connection_value is not None:
            await connection_value.close()


async def probe_onebot_boundary(url: str, token: str) -> None:
    """Require unauthenticated rejection and authenticated connection success."""
    unauthenticated_rejected = False
    try:
        await _connect_once(url, None)
    except Exception:  # noqa: BLE001 - every handshake/close rejection is acceptable here
        unauthenticated_rejected = True
    if not unauthenticated_rejected:
        raise QQBoundaryError(
            "qq_onebot_accepts_unauthenticated",
            "OneBot rejected boundary check: unauthenticated WebSocket was accepted",
        )
    try:
        await _connect_once(url, token)
    except Exception as exc:  # noqa: BLE001 - normalize library/network errors
        raise QQBoundaryError(
            "qq_onebot_authenticated_probe_failed",
            f"authenticated OneBot WebSocket probe failed ({type(exc).__name__})",
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate/probe the QQ OneBot boundary")
    parser.add_argument("action", choices=("validate-url", "validate", "probe"))
    parser.add_argument("--url", required=True)
    parser.add_argument("--url-env-key", default="QQ_WS_URL")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        url = require_loopback_websocket_url(args.url, env_key=args.url_env_key)
        if args.action == "validate-url":
            print(f"[OK] QQ OneBot loopback URL valid; env_key={args.url_env_key}")
            return 0
        token = require_access_token(os.environ.get("QQ_ACCESS_TOKEN"))
        if args.action == "probe":
            asyncio.run(probe_onebot_boundary(url, token))
    except QQBoundaryError as exc:
        print(f"[ERR] {exc.error_code}: {exc}")
        return 1
    action = "boundary-probe-ok" if args.action == "probe" else "config-ok"
    print(f"[OK] QQ OneBot {action}; token_length={len(token)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "QQBoundaryError",
    "main",
    "probe_onebot_boundary",
    "require_access_token",
    "require_loopback_websocket_url",
]
