"""Bounded NapCat v4.18.8 WebUI session and QQ login checks."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import math
import re
import subprocess
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

_CONTAINER_RE = re.compile(r"napcat-(?=.{1,63}\Z)[a-z0-9]+(?:-[a-z0-9]+)*")
_WEBUI_TOKEN_RE = re.compile(r"WebUi Token:\s*([^\s]+)")
_WEBUI_URL_RE = re.compile(r"WebUi User Panel Url:\s*(https?://[^\s]+)")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_LOG_CHARS = 256 * 1024
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_HTTP_BODY_BYTES = 64 * 1024
_MAX_TOKEN_CHARS = 512
_MAX_CREDENTIAL_CHARS = 16 * 1024
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_WEBUI_CONFIG_PATH = "/app/napcat/config/webui.json"


class NapCatWebUiError(RuntimeError):
    """Expose a stable, secret-free failure from a local NapCat WebUI check."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class WebUiSession:
    container: str
    host: str
    port: int
    token: str = field(repr=False)
    url: str = field(repr=False)
    running: bool


@dataclass(frozen=True)
class QQLoginStatus:
    is_login: bool
    is_offline: bool
    login_error: str
    qrcode_available: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "is_login": self.is_login,
            "is_offline": self.is_offline,
            "login_error": self.login_error,
            "qrcode_available": self.qrcode_available,
        }


def _validate_container(container: str) -> str:
    normalized = container.strip()
    if not _CONTAINER_RE.fullmatch(normalized):
        raise NapCatWebUiError(
            "invalid_container",
            "NapCat container must use the canonical napcat-<bot-id> name",
        )
    return normalized


def _validate_endpoint(host: str, port: int | str) -> tuple[str, int]:
    normalized_host = host.strip().lower()
    if normalized_host not in _LOOPBACK_HOSTS:
        raise NapCatWebUiError(
            "invalid_host",
            "NapCat WebUI host must be localhost, 127.0.0.1, or ::1",
        )
    try:
        normalized_port = int(port)
    except (TypeError, ValueError) as exc:
        raise NapCatWebUiError(
            "invalid_port",
            "NapCat WebUI port must be an integer",
        ) from exc
    if not 1 <= normalized_port <= 65535:
        raise NapCatWebUiError(
            "invalid_port",
            "NapCat WebUI port must be between 1 and 65535",
        )
    return normalized_host, normalized_port


def _docker_running(container: str) -> bool:
    try:
        completed = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NapCatWebUiError(
            "docker_unavailable",
            "Unable to inspect the canonical NapCat container",
        ) from exc
    if completed.returncode != 0:
        raise NapCatWebUiError(
            "container_not_found",
            f"NapCat container not found: {container}",
        )
    return completed.stdout.strip().lower() == "true"


def _read_bounded_logs(container: str) -> str:
    try:
        completed = subprocess.run(
            ["docker", "logs", "--tail", "300", container],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NapCatWebUiError(
            "docker_logs_failed",
            "Unable to read recent NapCat container logs",
        ) from exc
    if completed.returncode != 0:
        raise NapCatWebUiError(
            "docker_logs_failed",
            "Unable to read recent NapCat container logs",
        )
    text = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return _ANSI_ESCAPE_RE.sub("", text[-_MAX_LOG_CHARS:])


def _read_webui_config_token(container: str) -> str | None:
    script = (
        "import json,sys;"
        f"p=open({_WEBUI_CONFIG_PATH!r},'rb');"
        f"d=p.read({_MAX_CONFIG_BYTES + 1});p.close();"
        f"len(d)<={_MAX_CONFIG_BYTES} or sys.exit(2);"
        "v=json.loads(d.decode('utf-8')).get('token','');"
        "isinstance(v,str) or sys.exit(2);"
        "print(v,end='')"
    )
    try:
        completed = subprocess.run(
            ["docker", "exec", container, "python3", "-c", script],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _valid_token(completed.stdout)


def _valid_token(value: str) -> str:
    token = value.strip()
    if not token or len(token) > _MAX_TOKEN_CHARS:
        return ""
    if any(character.isspace() or ord(character) < 0x20 for character in token):
        return ""
    return token


def _token_from_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            return ""
        values = parse_qs(parsed.query, keep_blank_values=False).get("token", [])
    except ValueError:
        return ""
    return _valid_token(values[-1]) if values else ""


def _token_from_logs(text: str) -> str:
    tokens = [
        (match.start(), _valid_token(match.group(1)))
        for match in _WEBUI_TOKEN_RE.finditer(text)
    ]
    for match in _WEBUI_URL_RE.finditer(text):
        tokens.append((match.start(), _token_from_url(match.group(1))))
    return next((token for _, token in sorted(tokens, reverse=True) if token), "")


def webui_entrypoint_url(host: str, port: int | str) -> str:
    """Build the tokenless browser entrypoint for one loopback NapCat WebUI."""
    normalized_host, normalized_port = _validate_endpoint(host, port)
    rendered_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    return f"http://{rendered_host}:{normalized_port}/webui"


def _local_webui_url(host: str, port: int, token: str) -> str:
    return f"{webui_entrypoint_url(host, port)}?{urlencode({'token': token})}"


def read_webui_session(
    container: str,
    *,
    host: str = "localhost",
    port: int | str = 6099,
) -> WebUiSession:
    """Read the current canonical-container token and build a loopback URL."""
    normalized_container = _validate_container(container)
    normalized_host, normalized_port = _validate_endpoint(host, port)
    running = _docker_running(normalized_container)
    token = _read_webui_config_token(normalized_container) if running else None
    if token is None:
        token = _token_from_logs(_read_bounded_logs(normalized_container))
    if not token:
        raise NapCatWebUiError(
            "token_not_found",
            "NapCat WebUI token was not found in current config or bounded logs",
        )
    return WebUiSession(
        container=normalized_container,
        host=normalized_host,
        port=normalized_port,
        token=token,
        url=_local_webui_url(normalized_host, normalized_port, token),
        running=running,
    )


def webui_port_ready(host: str, port: int | str, *, timeout: float = 0.5) -> bool:
    """Check only the validated loopback TCP endpoint without sending credentials."""
    normalized_host, normalized_port = _validate_endpoint(host, port)
    connection = http.client.HTTPConnection(
        normalized_host,
        normalized_port,
        timeout=max(0.05, min(float(timeout), 5.0)),
    )
    try:
        connection.connect()
    except (OSError, ValueError):
        connection.close()
        return False
    else:
        connection.close()
        return True


def _post_json(
    host: str,
    port: int,
    path: str,
    payload: dict[str, Any],
    *,
    credential: str = "",
    timeout: float = 5.0,
) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(_MAX_HTTP_BODY_BYTES + 1)
    except (OSError, http.client.HTTPException):
        raise NapCatWebUiError(
            "webui_unreachable",
            "NapCat WebUI did not accept the local request",
        ) from None
    finally:
        connection.close()
    if response.status != 200:
        raise NapCatWebUiError(
            "webui_http_error",
            "NapCat WebUI returned an unexpected HTTP status",
        )
    if len(raw) > _MAX_HTTP_BODY_BYTES:
        raise NapCatWebUiError(
            "webui_response_too_large",
            "NapCat WebUI response exceeded the allowed size",
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NapCatWebUiError(
            "webui_invalid_response",
            "NapCat WebUI returned invalid JSON",
        ) from exc
    if not isinstance(parsed, dict):
        raise NapCatWebUiError(
            "webui_invalid_response",
            "NapCat WebUI returned an invalid response object",
        )
    return parsed


def _authenticate(session: WebUiSession, *, timeout: float) -> str:
    token_hash = hashlib.sha256(f"{session.token}.napcat".encode("utf-8")).hexdigest()
    response = _post_json(
        session.host,
        session.port,
        "/api/auth/login",
        {"hash": token_hash},
        timeout=timeout,
    )
    if response.get("code") != 0:
        raise NapCatWebUiError(
            "webui_auth_failed",
            "NapCat WebUI rejected the local authentication request",
        )
    data = response.get("data")
    if not isinstance(data, dict):
        raise NapCatWebUiError(
            "webui_auth_failed",
            "NapCat WebUI authentication returned no credential",
        )
    if data.get("require2FA") is True:
        raise NapCatWebUiError(
            "webui_two_factor_required",
            "NapCat WebUI requires interactive two-factor authentication",
        )
    credential = data.get("Credential")
    if (
        not isinstance(credential, str)
        or not credential
        or len(credential) > _MAX_CREDENTIAL_CHARS
        or re.fullmatch(r"[A-Za-z0-9+/=]+", credential) is None
    ):
        raise NapCatWebUiError(
            "webui_auth_failed",
            "NapCat WebUI authentication returned no usable credential",
        )
    return credential


def _public_text(value: object, *, secrets: tuple[str, ...]) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split())[:256]
    for secret in secrets:
        if secret:
            normalized = normalized.replace(secret, "[redacted]")
    return normalized


def _check_login_with_credential(
    session: WebUiSession,
    credential: str,
    *,
    timeout: float,
) -> QQLoginStatus:
    response = _post_json(
        session.host,
        session.port,
        "/api/QQLogin/CheckLoginStatus",
        {},
        credential=credential,
        timeout=timeout,
    )
    if response.get("code") != 0 or not isinstance(response.get("data"), dict):
        raise NapCatWebUiError(
            "login_status_failed",
            "NapCat WebUI rejected the QQ login status request",
        )
    data = response["data"]
    return QQLoginStatus(
        is_login=data.get("isLogin") is True,
        is_offline=data.get("isOffline") is True,
        login_error=_public_text(
            data.get("loginError"),
            secrets=(session.token, credential),
        ),
        qrcode_available=bool(data.get("qrcodeurl")),
    )


def check_login_status(
    session: WebUiSession,
    *,
    timeout: float = 5.0,
) -> QQLoginStatus:
    """Authenticate once and query the v4.18.8 QQ login status endpoint."""
    credential = _authenticate(session, timeout=timeout)
    return _check_login_with_credential(session, credential, timeout=timeout)


def wait_for_login_status(
    session: WebUiSession,
    *,
    wait_seconds: float,
    interval_seconds: float = 2.0,
    request_timeout: float = 5.0,
) -> QQLoginStatus:
    """Reuse one in-memory credential for bounded status polling."""
    normalized_wait = float(wait_seconds)
    normalized_interval = float(interval_seconds)
    if not math.isfinite(normalized_wait) or not math.isfinite(normalized_interval):
        raise NapCatWebUiError(
            "invalid_wait",
            "NapCat login wait and interval must be finite numbers",
        )
    bounded_wait = max(0.0, min(normalized_wait, 600.0))
    bounded_interval = max(0.25, min(normalized_interval, 30.0))
    credential = _authenticate(session, timeout=request_timeout)
    deadline = time.monotonic() + bounded_wait
    while True:
        status = _check_login_with_credential(
            session,
            credential,
            timeout=request_timeout,
        )
        if status.is_login or time.monotonic() >= deadline:
            return status
        time.sleep(min(bounded_interval, max(0.0, deadline - time.monotonic())))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect a local NapCat WebUI session")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("webui-url", "login-status"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--container", required=True)
        command_parser.add_argument("--host", default="localhost")
        command_parser.add_argument("--port", default="6099")
        command_parser.add_argument("--json", action="store_true")
        if command == "login-status":
            command_parser.add_argument("--wait-seconds", type=float, default=0.0)
            command_parser.add_argument("--interval-seconds", type=float, default=2.0)
    return parser


def _print_result(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return
    if "url" in result:
        print(result["url"])
    elif result.get("is_login"):
        print("logged_in")
    else:
        print("logged_out")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        session = read_webui_session(
            args.container,
            host=args.host,
            port=args.port,
        )
        if args.command == "webui-url":
            result = {
                "ok": True,
                "url": session.url,
                "container": session.container,
                "running": session.running,
            }
            exit_code = 0
        else:
            status = wait_for_login_status(
                session,
                wait_seconds=args.wait_seconds,
                interval_seconds=args.interval_seconds,
            )
            result = status.to_dict()
            exit_code = 0 if status.is_login else 3
    except NapCatWebUiError as exc:
        result = {
            "ok": False,
            "error_code": exc.error_code,
            "message": str(exc),
        }
        exit_code = 1
    _print_result(result, as_json=args.json)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
