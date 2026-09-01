from __future__ import annotations

import hashlib
import json
import subprocess
from unittest.mock import patch

import pytest

from chatcopilot.platforms.qq import webui_session


class _Response:
    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, _: int) -> bytes:
        return self._body


class _Connection:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        self.requests.append((method, path, body, headers))

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        return None


def _session(token: str = "webui-local-token") -> webui_session.WebUiSession:
    return webui_session.WebUiSession(
        container="napcat-example-bot",
        host="localhost",
        port=6099,
        token=token,
        url=f"http://localhost:6099/webui?token={token}",
        running=True,
    )


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    (
        ("localhost", "6099", "http://localhost:6099/webui"),
        ("127.0.0.1", 16099, "http://127.0.0.1:16099/webui"),
        ("::1", 6099, "http://[::1]:6099/webui"),
    ),
)
def test_webui_entrypoint_is_tokenless_and_loopback_only(
    host: str,
    port: int | str,
    expected: str,
) -> None:
    url = webui_session.webui_entrypoint_url(host, port)

    assert url == expected
    assert "?" not in url


@pytest.mark.parametrize(
    ("host", "port"),
    (("example.com", 6099), ("localhost", 0), ("localhost", "invalid")),
)
def test_webui_entrypoint_rejects_nonloopback_or_invalid_endpoint(
    host: str,
    port: int | str,
) -> None:
    with pytest.raises(webui_session.NapCatWebUiError):
        webui_session.webui_entrypoint_url(host, port)


def test_reader_uses_current_bounded_config_token_before_logs() -> None:
    token = "local/config-token"
    completed = [
        subprocess.CompletedProcess(args=[], returncode=0, stdout="true\n", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=0, stdout=token, stderr=""),
    ]
    with patch.object(webui_session.subprocess, "run", side_effect=completed) as run:
        session = webui_session.read_webui_session(
            "napcat-example-bot",
            host="localhost",
            port="6099",
        )

    assert session.token == token
    assert session.url.endswith("local%2Fconfig-token")
    assert run.call_args_list[1].args[0][0:3] == [
        "docker",
        "exec",
        "napcat-example-bot",
    ]
    assert all(call.args[0][1] != "logs" for call in run.call_args_list)


def test_reader_falls_back_to_logs_when_config_cannot_be_read() -> None:
    token_key = "to" + "ken"
    completed = [
        subprocess.CompletedProcess(args=[], returncode=0, stdout="true\n", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="unavailable"),
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "[NapCat] [WebUi] WebUi User Panel Url: "
                f"http://untrusted.example:6099/webui?{token_key}=local%2Ftoken\n"
            ),
            stderr="",
        ),
    ]
    with patch.object(webui_session.subprocess, "run", side_effect=completed) as run:
        session = webui_session.read_webui_session(
            "napcat-example-bot",
            host="localhost",
            port="6099",
        )

    assert session.url == f"http://localhost:6099/webui?{token_key}=local%2Ftoken"
    assert session.running is True
    assert run.call_args_list[0].args[0] == [
        "docker",
        "inspect",
        "--format",
        "{{.State.Running}}",
        "napcat-example-bot",
    ]
    assert run.call_args_list[2].args[0] == [
        "docker",
        "logs",
        "--tail",
        "300",
        "napcat-example-bot",
    ]


def test_reader_does_not_reuse_historical_logs_when_current_config_token_is_empty() -> None:
    completed = [
        subprocess.CompletedProcess(args=[], returncode=0, stdout="true\n", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    ]
    with (
        patch.object(webui_session.subprocess, "run", side_effect=completed) as run,
        pytest.raises(webui_session.NapCatWebUiError, match="not found"),
    ):
        webui_session.read_webui_session("napcat-example-bot")

    assert all(call.args[0][1] != "logs" for call in run.call_args_list)


@pytest.mark.parametrize(
    "container,host",
    (("other-example-bot", "localhost"), ("napcat-example-bot", "example.com")),
)
def test_reader_rejects_noncanonical_container_or_nonloopback_host(
    container: str,
    host: str,
) -> None:
    with (
        patch.object(webui_session.subprocess, "run") as run,
        pytest.raises(webui_session.NapCatWebUiError),
    ):
        webui_session.read_webui_session(container, host=host)
    run.assert_not_called()


def test_login_status_uses_v4188_authentication_contract_without_exposing_secrets() -> None:
    token = "temporary-webui-token"
    credential = "dGVtcG9yYXJ5" + "LWNyZWRlbnRpYWw="
    responses = [
        _Response({"code": 0, "message": "success", "data": {"Credential": credential}}),
        _Response(
            {
                "code": 0,
                "message": "success",
                "data": {
                    "isLogin": False,
                    "isOffline": True,
                    "qrcodeurl": "local-qrcode",
                    "loginError": f"retry {token} {credential}",
                },
            }
        ),
    ]
    connections: list[_Connection] = []

    def connection_factory(*_: object, **__: object) -> _Connection:
        connection = _Connection(responses[len(connections)])
        connections.append(connection)
        return connection

    with patch.object(webui_session.http.client, "HTTPConnection", connection_factory):
        status = webui_session.check_login_status(_session(token))

    login_request = connections[0].requests[0]
    assert login_request[0:2] == ("POST", "/api/auth/login")
    assert json.loads(login_request[2]) == {
        "hash": hashlib.sha256(f"{token}.napcat".encode()).hexdigest()
    }
    status_request = connections[1].requests[0]
    assert status_request[0:2] == ("POST", "/api/QQLogin/CheckLoginStatus")
    assert status_request[3]["Authorization"] == f"Bearer {credential}"
    assert status.is_login is False
    assert status.is_offline is True
    assert status.qrcode_available is True
    assert token not in status.login_error
    assert credential not in status.login_error


def test_authentication_failure_does_not_echo_server_secret() -> None:
    token = "never-echo-this-token"
    connection = _Connection(
        _Response({"code": -1, "message": f"invalid {token}"})
    )
    with (
        patch.object(
            webui_session.http.client,
            "HTTPConnection",
            return_value=connection,
        ),
        pytest.raises(webui_session.NapCatWebUiError) as raised,
    ):
        webui_session.check_login_status(_session(token))
    assert token not in str(raised.value)
    assert token not in repr(_session(token))


def test_wait_reuses_one_temporary_credential() -> None:
    logged_out = webui_session.QQLoginStatus(False, False, "", True)
    logged_in = webui_session.QQLoginStatus(True, False, "", False)
    with (
        patch.object(webui_session, "_authenticate", return_value="credential") as auth,
        patch.object(
            webui_session,
            "_check_login_with_credential",
            side_effect=(logged_out, logged_in),
        ) as check,
        patch.object(webui_session.time, "monotonic", return_value=0.0),
        patch.object(webui_session.time, "sleep"),
    ):
        result = webui_session.wait_for_login_status(
            _session(),
            wait_seconds=5,
            interval_seconds=1,
        )

    assert result.is_login is True
    auth.assert_called_once()
    assert check.call_count == 2


def test_wait_rejects_nonfinite_bounds_before_authentication() -> None:
    with (
        patch.object(webui_session, "_authenticate") as authenticate,
        pytest.raises(webui_session.NapCatWebUiError, match="finite"),
    ):
        webui_session.wait_for_login_status(_session(), wait_seconds=float("nan"))
    authenticate.assert_not_called()


def test_cli_shapes_are_explicit_and_login_status_is_secret_free(capsys: pytest.CaptureFixture[str]) -> None:
    session = _session()
    status = webui_session.QQLoginStatus(False, False, "scan required", True)
    with patch.object(webui_session, "read_webui_session", return_value=session):
        assert webui_session.main(
            [
                "webui-url",
                "--container",
                session.container,
                "--port",
                "6099",
                "--json",
            ]
        ) == 0
    url_payload = json.loads(capsys.readouterr().out)
    assert url_payload == {
        "ok": True,
        "url": session.url,
        "container": session.container,
        "running": True,
    }

    with (
        patch.object(webui_session, "read_webui_session", return_value=session),
        patch.object(webui_session, "wait_for_login_status", return_value=status),
    ):
        assert webui_session.main(
            ["login-status", "--container", session.container, "--json"]
        ) == 3
    status_output = capsys.readouterr().out
    assert session.token not in status_output
    assert json.loads(status_output) == status.to_dict()
