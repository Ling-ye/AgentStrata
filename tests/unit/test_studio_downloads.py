"""The accepted digest must bind the bytes sent, not a later reopening of a path."""
import asyncio
import builtins
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.responses import FileResponse

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = bytes(range(256)) * 817


def make_response(tmp_path):
    from studio.downloads import VerifiedBuildResponse
    path = tmp_path / "application.exe"
    path.write_bytes(PAYLOAD)
    return path, VerifiedBuildResponse(path, hashlib.sha256(PAYLOAD).hexdigest(), filename="AgentStrata-test.exe")


def run_response(response, *, headers=(), method="GET", hook=None, extensions=None):
    messages = []

    async def send(message):
        if hook:
            hook(message)
        messages.append(message)

    async def receive():
        return {"type": "http.disconnect"}

    asyncio.run(response({"type": "http", "method": method, "headers": headers,
                          "extensions": extensions or {}}, receive, send))
    return messages


def body(messages):
    return b"".join(m.get("body", b"") for m in messages)


@pytest.mark.parametrize("entry", ["legacy", "unified"])
@pytest.mark.parametrize("mutation", ["replace", "overwrite"])
def test_actual_download_route_never_sends_a_replacement_after_digest_check(tmp_path, monkeypatch, entry, mutation):
    from fastapi.testclient import TestClient
    from studio.console_server import application as unified
    from studio.server import application as legacy
    home = tmp_path / "studio"
    app = legacy(home, ROOT, "private-test-token", run_worker=False) if entry == "legacy" else unified(home, ROOT, "private-test-token")
    if entry == "unified":
        app.state.applications._discover = lambda: []
        studio = app.state.applications.studio()
    else:
        studio = app.state.studio
    project, message, other = uuid4().hex, uuid4().hex, uuid4().hex
    studio.create(project)
    studio.create(other)
    studio.submit(project, message, "Inert build fixture; no worker or model is run")
    studio.update(message, "ready", "Fixture", build=message)
    build = studio.directory(project) / "builds" / message
    build.mkdir(parents=True)
    installer = build / "application.exe"
    installer.write_bytes(PAYLOAD)
    (build / "build-receipt.json").write_text(json.dumps({"installer_sha256": hashlib.sha256(PAYLOAD).hexdigest()}))
    changed = b"x" * len(PAYLOAD)
    previous = FileResponse.__call__
    fired = []

    async def race(response, scope, receive, send):
        if Path(response.path) == installer:
            fired.append(True)
            if mutation == "replace":
                replacement = build / "replacement.bin"
                replacement.write_bytes(changed)
                try:
                    os.replace(replacement, installer)
                except PermissionError:
                    # Windows may deny replacement while the checked handle is held.
                    assert os.name == "nt"
            else:
                installer.write_bytes(changed)
        return await previous(response, scope, receive, send)

    monkeypatch.setattr(FileResponse, "__call__", race)
    # Exercise the real routes without starting models, workers or system services.
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345),
                        raise_server_exceptions=False)
    try:
        assert client.post("/api/login", json={"token": "private-test-token"}).status_code == 200
        prefix = "/api/projects/" if entry == "legacy" else "/api/applications/studio:"
        response = client.get(prefix + project + "/builds/" + message)
        assert fired
        if mutation == "replace":
            assert response.status_code == 200 and response.content == PAYLOAD
        else:
            assert response.content == PAYLOAD or len(response.content) < len(PAYLOAD)
            assert response.content != changed
        assert client.get(prefix + other + "/builds/" + message).status_code == 404
    finally:
        client.close()


@pytest.mark.parametrize("range_header, expected", [
    (b"bytes=0-15", PAYLOAD[:16]),
    (b"bytes=-13", PAYLOAD[-13:]),
    (b"bytes=65530-65549", PAYLOAD[65530:65550]),
    (b"bytes=65536-", PAYLOAD[65536:]),
], ids=["prefix", "suffix", "cross-chunk", "open-ended"])
def test_verified_ranges_keep_exact_payload_and_close_handle(tmp_path, range_header, expected):
    _, response = make_response(tmp_path)
    messages = run_response(response, headers=[(b"range", range_header)])
    assert messages[0]["status"] == 206 and body(messages) == expected
    assert dict(messages[0]["headers"])[b"content-length"] == str(len(expected)).encode()
    assert response._file is None


def test_full_and_multiple_ranges_never_reopen_or_use_pathsend(tmp_path, monkeypatch):
    import anyio
    path, response = make_response(tmp_path)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("The mutable path must never be reopened")

    monkeypatch.setattr(anyio, "open_file", forbidden)
    messages = run_response(response, extensions={"http.response.pathsend": {}})
    assert body(messages) == PAYLOAD
    assert all(m["type"] != "http.response.pathsend" for m in messages)
    assert response._file is None
    from studio.downloads import VerifiedBuildResponse
    response = VerifiedBuildResponse(path, hashlib.sha256(PAYLOAD).hexdigest(), filename="test.exe")
    messages = run_response(response, headers=[(b"range", b"bytes=0-3,65530-65549")])
    content = body(messages)
    headers = dict(messages[0]["headers"])
    assert messages[0]["status"] == 206 and headers[b"content-type"].startswith(b"multipart/byteranges;")
    assert len(content) == int(headers[b"content-length"])
    assert b"Content-Range: bytes 0-3/" in content and PAYLOAD[:4] in content
    assert b"Content-Range: bytes 65530-65549/" in content and PAYLOAD[65530:65550] in content
    assert response._file is None


@pytest.mark.parametrize("mutation", ["overwrite", "truncate"])
def test_in_place_change_during_transfer_never_sends_the_changed_chunk(tmp_path, mutation):
    path, response = make_response(tmp_path)
    sent = []

    def mutate(message):
        if message["type"] == "http.response.body" and message.get("body") and not sent:
            sent.append(message["body"])
            with path.open("r+b") as stream:
                if mutation == "truncate":
                    stream.truncate(response.chunk_size + 5)
                else:
                    stream.seek(response.chunk_size)
                    stream.write(b"x" * response.chunk_size)

    with pytest.raises(ValueError, match="下载时发生变化"):
        run_response(response, hook=mutate)
    assert sent == [PAYLOAD[:response.chunk_size]]
    assert response._file is None


def test_modified_range_chunk_is_checked_before_any_of_it_is_sent(tmp_path):
    path, response = make_response(tmp_path)
    with path.open("r+b") as stream:
        stream.seek(65536 + 200)
        stream.write(b"corrupted")
    with pytest.raises(ValueError, match="下载时发生变化"):
        run_response(response, headers=[(b"range", b"bytes=65536-65539")])
    assert response._file is None


@pytest.mark.parametrize("range_header, expected_status", [(b"invalid", 400), (b"bytes=9999999-", 416)])
def test_invalid_ranges_release_checked_handle(tmp_path, range_header, expected_status):
    _, response = make_response(tmp_path)
    messages = run_response(response, headers=[(b"range", range_header)])
    assert messages[0]["status"] == expected_status and response._file is None


@pytest.mark.parametrize("matches", [False, True])
def test_if_range_uses_the_verified_digest(tmp_path, matches):
    _, response = make_response(tmp_path)
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    messages = run_response(response, headers=[(b"range", b"bytes=0-3"),
        (b"if-range", ('"' + (digest if matches else "different") + '"').encode())])
    assert messages[0]["status"] == (206 if matches else 200)
    assert body(messages) == (PAYLOAD[:4] if matches else PAYLOAD)
    assert dict(messages[0]["headers"])[b"etag"] == ('"' + digest + '"').encode()
    assert response._file is None


@pytest.mark.parametrize("headers", [[], [(b"range", b"bytes=0-3,65536-65539")]])
def test_head_releases_handle_without_sending_file_bytes(tmp_path, headers):
    _, response = make_response(tmp_path)
    messages = run_response(response, method="HEAD", headers=headers)
    assert not body(messages) and response._file is None


@pytest.mark.parametrize("exception", [OSError("controlled disconnect"), asyncio.CancelledError()])
def test_disconnect_and_cancellation_release_handle(tmp_path, exception):
    _, response = make_response(tmp_path)

    def disconnect(message):
        if message["type"] == "http.response.body":
            raise exception

    with pytest.raises(type(exception)):
        run_response(response, hook=disconnect)
    assert response._file is None


def test_wrong_digest_closes_initial_handle_before_any_response(tmp_path, monkeypatch):
    from studio import downloads
    opened = []

    def track(*args, **kwargs):
        file = builtins.open(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(downloads, "open", track, raising=False)
    path = tmp_path / "application.exe"
    path.write_bytes(PAYLOAD)
    with pytest.raises(ValueError, match="摘要不一致"):
        downloads.VerifiedBuildResponse(path, "0" * 64, filename="test.exe")
    assert len(opened) == 1 and opened[0].closed
