"""Readiness must identify the exact process without disclosing its launch token."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

from fastapi.testclient import TestClient
import pytest
import uvicorn

from studio.launcher import READINESS_PATH, ready_url

TOKEN = "synthetic-launch-token-for-readiness"
ROOT = Path(__file__).resolve().parents[2]


def app_for(kind, tmp_path, token=TOKEN):
    if kind == "console":
        from studio.console_server import application
        return application(tmp_path, ROOT, token)
    from studio.server import application
    return application(tmp_path, ROOT, token, run_worker=False)


@contextmanager
def live_server(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                          lifespan="off", access_log=False, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()


def record(home, port, *, token=TOKEN, pid=None):
    url = f"http://127.0.0.1:{port}/#login={token}"
    (home / "launcher.json").write_text(json.dumps({"pid": os.getpid() if pid is None else pid, "url": url}))
    return url


@pytest.mark.parametrize("kind", ["console", "legacy"])
def test_running_entry_proves_its_identity_without_session_or_token_in_request(tmp_path, monkeypatch, kind):
    app = app_for(kind, tmp_path)
    requests = []
    @app.middleware("http")
    async def observe(request, call_next):
        requests.append(str(request.url))
        return await call_next(request)
    # The probe must bypass proxy settings even on hosts that proxy all HTTP.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    with live_server(app) as port:
        url = record(tmp_path, port)
        assert ready_url(tmp_path, expected_pid=os.getpid()) == url
        assert ready_url(tmp_path, expected_pid=os.getpid()) == url
    assert len(requests) == 2 and requests[0] != requests[1]
    assert all(TOKEN not in request and "#login" not in request for request in requests)


@pytest.mark.parametrize("kind", ["console", "legacy"])
def test_stale_token_or_pid_cannot_prove_readiness(tmp_path, kind):
    with live_server(app_for(kind, tmp_path)) as port:
        record(tmp_path, port, token="different-stale-token-for-readiness")
        with pytest.raises(ValueError, match="does not match"):
            ready_url(tmp_path)
        record(tmp_path, port, pid=os.getpid() + 1)
        with pytest.raises(ValueError, match="does not match"):
            ready_url(tmp_path)
        record(tmp_path, port)
        with pytest.raises(ValueError, match="does not match"):
            ready_url(tmp_path, expected_pid=os.getpid() + 1)


@pytest.mark.parametrize("kind", ["console", "legacy"])
def test_readiness_does_not_authorize_other_routes(tmp_path, kind):
    client = TestClient(app_for(kind, tmp_path), base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    response = client.get(READINESS_PATH, params={"challenge": "a" * 64})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert TOKEN not in response.text
    assert client.get("/api/applications" if kind == "console" else "/api/projects").status_code == 401
    assert client.get(READINESS_PATH, params={"challenge": "invalid"}).status_code == 400
    assert client.get(READINESS_PATH, params={"challenge": "a" * 64},
                      headers={"Origin": "https://example.invalid"}).status_code == 403


@pytest.mark.parametrize("url", ["https://example.invalid/#login=synthetic-token-value",
                                  "http://127.0.0.1:70000/#login=synthetic-token-value",
                                  "http://localhost:18636/#login=synthetic-token-value",
                                  "http://127.0.0.1:18636/path#login=synthetic-token-value"])
def test_invalid_launcher_address_is_rejected_before_network(tmp_path, monkeypatch, url):
    from studio import launcher
    (tmp_path / "launcher.json").write_text(json.dumps({"pid": os.getpid(), "url": url}))
    monkeypatch.setattr(launcher, "build_opener", lambda *args: pytest.fail("invalid launcher made a network request"))
    with pytest.raises(ValueError, match="does not match"):
        ready_url(tmp_path)


@pytest.mark.parametrize("response_mode", ["unrelated", "redirect", "broken_chunk"])
def test_unrelated_http_service_or_redirect_cannot_prove_readiness(tmp_path, response_mode):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(302 if response_mode == "redirect" else 200)
            self.send_header("Location", "/unexpected-followup")
            if response_mode == "broken_chunk":
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b'10\r\n{' if response_mode == "broken_chunk" else b'{"ok":true}')
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        record(tmp_path, server.server_port)
        with pytest.raises(ValueError, match="does not match"):
            ready_url(tmp_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert len(requests) == 1
    assert TOKEN not in requests[0]


def test_missing_or_malformed_record_has_sanitized_failure(tmp_path):
    for value in (None, "{invalid", '[]', '{"pid":true,"url":"private-value"}'):
        file = tmp_path / "launcher.json"
        if value is not None:
            file.write_text(value)
        with pytest.raises(ValueError, match="^Local Studio is not ready or does not match its launcher record$"):
            ready_url(tmp_path)


def test_url_cli_prints_only_verified_address_and_sanitizes_failures(tmp_path):
    with live_server(app_for("console", tmp_path)) as port:
        url = record(tmp_path, port)
        command = [sys.executable, "-m", "studio", "url", "--home", str(tmp_path), "--expected-pid"]
        ready = subprocess.run([*command, str(os.getpid())], cwd=ROOT, text=True,
                               capture_output=True, timeout=10)
        assert ready.returncode == 0, ready.stderr
        assert ready.stdout.strip() == url
        stale = subprocess.run([*command, str(os.getpid() + 1)], cwd=ROOT, text=True,
                               capture_output=True, timeout=10)
        assert stale.returncode == 1
        assert stale.stdout == ""
        assert "does not match its launcher record" in stale.stderr
        assert TOKEN not in stale.stderr and "Traceback" not in stale.stderr
