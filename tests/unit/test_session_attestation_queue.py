from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

import pytest

from chatcopilot.botspec import cli as botspec_cli
from chatcopilot.contracts.identity import ConversationIdentity, TurnIdentity
from chatcopilot.middleware.acp import agent_bridge


REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_SPEC = REPO_ROOT / "bots" / "lingye-copilot-qq" / "bot.yaml"
GROUP_ID = "30003"
SESSION_KEY = f"qq:g:{GROUP_ID}"


def _identity_values() -> dict[str, str]:
    return {
        "CHATCOPILOT_USER_ID": "",
        "CHATCOPILOT_CHAT_ID": GROUP_ID,
        "CHATCOPILOT_CHAT_KIND": "group",
        "CHATCOPILOT_USER_NAME": "",
    }


def _attested_values(actor: str, text: str) -> dict[str, str]:
    values = _identity_values()
    values.update(
        {
            "CHATCOPILOT_TRANSPORT_HOOK_EVENT": "message.received",
            "CHATCOPILOT_TRANSPORT_USER_ID": actor,
            "CHATCOPILOT_TRANSPORT_CONTENT_SHA256": hashlib.sha256(
                text.strip().encode("utf-8")
            ).hexdigest(),
        }
    )
    return values


def _append(directory: Path, *, actor: str, text: str) -> Path:
    return botspec_cli._write_private_session_env(
        directory=directory,
        session_key=SESSION_KEY,
        values=_attested_values(actor, text),
    )


def _path(directory: Path, session_key: str = SESSION_KEY) -> Path:
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    return directory / f"cc-sess-{digest}.env"


def _lock_path(directory: Path, session_key: str = SESSION_KEY) -> Path:
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    return directory / f"cc-sess-{digest}.lock"


def _state(directory: Path, session_key: str = SESSION_KEY) -> dict[str, object]:
    return json.loads(_path(directory, session_key).read_text(encoding="utf-8"))


def _turn(actor: str) -> TurnIdentity:
    return TurnIdentity(
        conversation=ConversationIdentity(
            platform="qq",
            chat_kind="group",
            chat_id=GROUP_ID,
        ),
        sender_user_id=actor,
        source="cc-connect-sender-envelope",
    )


def _bind_runtime_env(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    monkeypatch.setenv("CHATCOPILOT_SESSION_ENV_DIR", str(directory))
    monkeypatch.setenv("CC_SESSION_KEY", SESSION_KEY)


def test_cross_process_append_preserves_every_unique_and_duplicate_record(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session-env"
    messages = [
        ("20002", "same text"),
        ("20002", "same text"),
        ("20003", "other actor"),
        ("20004", "message four"),
        ("20005", "message five"),
        ("20006", "message six"),
        ("20007", "message seven"),
        ("20008", "message eight"),
    ]

    def invoke(item: tuple[str, str]) -> subprocess.CompletedProcess[str]:
        actor, text = item
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": str(REPO_ROOT / "src"),
                "CC_HOOK_EVENT": "message.received",
                "CC_HOOK_USER_ID": actor,
                "CC_HOOK_CONTENT": text,
            }
        )
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "chatcopilot",
                "bot",
                "render-session-env",
                "--bot",
                str(BOT_SPEC),
                "--session-key",
                SESSION_KEY,
                "--session-env-dir",
                str(directory),
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=len(messages)) as pool:
        results = list(pool.map(invoke, messages))

    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results if result.returncode != 0
    ]
    payload = _state(directory)
    records = payload["attestations"]
    assert isinstance(records, list)
    assert len(records) == len(messages)
    assert len({record["record_id"] for record in records}) == len(messages)
    observed = sorted((record["transport_user_id"], record["content_sha256"]) for record in records)
    expected = sorted(
        (actor, hashlib.sha256(text.strip().encode("utf-8")).hexdigest())
        for actor, text in messages
    )
    assert observed == expected
    assert _path(directory).stat().st_mode & 0o777 == 0o600
    assert _lock_path(directory).stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700


def test_duplicate_actor_and_body_records_are_consumed_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session-env"
    path = _append(directory, actor="20002", text="duplicate")
    _append(directory, actor="20002", text="duplicate")
    before = _state(directory)
    record_ids = [record["record_id"] for record in before["attestations"]]
    assert len(record_ids) == 2
    assert record_ids[0] != record_ids[1]
    _bind_runtime_env(monkeypatch, directory)

    first = agent_bridge._validate_qq_group_transport_attestation(_turn("20002"), "duplicate")
    assert first is not None and first.content_digest_matches
    remaining = _state(directory)["attestations"]
    assert len(remaining) == 1
    assert remaining[0]["record_id"] == record_ids[1]

    second = agent_bridge._validate_qq_group_transport_attestation(_turn("20002"), "duplicate")
    assert second is not None and second.content_digest_matches
    assert _state(directory)["attestations"] == []
    assert path.exists()
    assert _state(directory)["identity"] == _identity_values()

    with pytest.raises(agent_bridge.TransportAttestationError) as replay:
        agent_bridge._validate_qq_group_transport_attestation(_turn("20002"), "duplicate")
    assert replay.value.code == "qq_transport_attestation_missing"


def test_concurrent_append_and_consume_never_loses_unconsumed_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session-env"
    count = 12
    for index in range(count):
        _append(directory, actor=f"old-{index}", text=f"old body {index}")
    _bind_runtime_env(monkeypatch, directory)

    def append_new(index: int) -> None:
        _append(directory, actor=f"new-{index}", text=f"new body {index}")

    def consume_old(index: int) -> None:
        result = agent_bridge._validate_qq_group_transport_attestation(
            _turn(f"old-{index}"), f"old body {index}"
        )
        assert result is not None and result.content_digest_matches

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(append_new, index) for index in range(count)]
        futures.extend(pool.submit(consume_old, index) for index in range(count))
        for future in futures:
            future.result(timeout=10)

    records = _state(directory)["attestations"]
    assert len(records) == count
    assert {record["transport_user_id"] for record in records} == {
        f"new-{index}" for index in range(count)
    }
    assert len({record["record_id"] for record in records}) == count


def test_matching_scan_never_consumes_cross_actor_or_wrong_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session-env"
    _append(directory, actor="20002", text="actor a")
    _append(directory, actor="20003", text="actor b")
    original_ids = [record["record_id"] for record in _state(directory)["attestations"]]
    _bind_runtime_env(monkeypatch, directory)

    with pytest.raises(agent_bridge.TransportAttestationError) as unknown_actor:
        agent_bridge._validate_qq_group_transport_attestation(_turn("20004"), "actor a")
    assert unknown_actor.value.code == "qq_transport_actor_mismatch"
    assert [record["record_id"] for record in _state(directory)["attestations"]] == original_ids

    with pytest.raises(agent_bridge.TransportAttestationError) as wrong_body:
        agent_bridge._validate_qq_group_transport_attestation(_turn("20002"), "wrong")
    assert wrong_body.value.code == "qq_transport_content_mismatch"
    assert [record["record_id"] for record in _state(directory)["attestations"]] == original_ids

    agent_bridge._validate_qq_group_transport_attestation(_turn("20003"), "actor b")
    remaining = _state(directory)["attestations"]
    assert [record["transport_user_id"] for record in remaining] == ["20002"]
    agent_bridge._validate_qq_group_transport_attestation(_turn("20002"), "actor a")
    assert _state(directory)["attestations"] == []


def test_queue_capacity_fails_closed_without_dropping_live_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session-env"
    monkeypatch.setattr(botspec_cli, "_MAX_SESSION_ATTESTATIONS", 3)
    for index in range(3):
        _append(directory, actor=f"2000{index + 2}", text=f"message {index}")
    before = _state(directory)

    monkeypatch.setenv("CC_HOOK_EVENT", "message.received")
    monkeypatch.setenv("CC_HOOK_USER_ID", "29999")
    monkeypatch.setenv("CC_HOOK_CONTENT", "must not be admitted")
    exit_code = botspec_cli.main(
        [
            "render-session-env",
            "--bot",
            str(BOT_SPEC),
            "--session-key",
            SESSION_KEY,
            "--session-env-dir",
            str(directory),
        ]
    )

    after = _state(directory)
    assert exit_code == 78
    assert after == before
    assert len(after["attestations"]) == 3
    assert all(record["transport_user_id"] != "29999" for record in after["attestations"])

    _bind_runtime_env(monkeypatch, directory)
    with pytest.raises(agent_bridge.TransportAttestationError) as rejected:
        agent_bridge._validate_qq_group_transport_attestation(
            _turn("29999"), "must not be admitted"
        )
    assert rejected.value.code == "qq_transport_actor_mismatch"
    assert _state(directory) == before


def test_non_group_transport_refresh_never_fills_pending_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session-env"
    session_key = "qq:20002"
    monkeypatch.setattr(botspec_cli, "_MAX_SESSION_ATTESTATIONS", 3)
    monkeypatch.setenv("CC_HOOK_EVENT", "message.received")
    monkeypatch.setenv("CC_HOOK_USER_ID", "20002")

    for index in range(5):
        monkeypatch.setenv("CC_HOOK_CONTENT", f"private message {index}")
        assert (
            botspec_cli.main(
                [
                    "render-session-env",
                    "--bot",
                    str(BOT_SPEC),
                    "--session-key",
                    session_key,
                    "--session-env-dir",
                    str(directory),
                ]
            )
            == 0
        )

    payload = _state(directory, session_key)
    assert payload["identity"]["CHATCOPILOT_CHAT_KIND"] == "p2p"
    assert len(payload["attestations"]) == 1
    assert (
        payload["attestations"][0]["content_sha256"]
        == hashlib.sha256(b"private message 4").hexdigest()
    )


def test_expired_records_are_pruned_but_live_records_are_never_evicted(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session-env"
    path = _append(directory, actor="20002", text="stale")
    payload = _state(directory)
    payload["attestations"][0]["created_at_ns"] = (
        time.time_ns() - botspec_cli._SESSION_ATTESTATION_TTL_NS - 1
    )
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    _append(directory, actor="20003", text="live")

    records = _state(directory)["attestations"]
    assert len(records) == 1
    assert records[0]["transport_user_id"] == "20003"


def test_state_copy_to_another_session_hash_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session-env"
    _append(directory, actor="20002", text="bound")
    other_key = "qq:g:39999"
    other_path = _path(directory, other_key)
    other_lock = _lock_path(directory, other_key)
    shutil.copyfile(_path(directory), other_path)
    other_path.chmod(0o600)
    other_lock.write_text("", encoding="utf-8")
    other_lock.chmod(0o600)
    monkeypatch.setenv("CHATCOPILOT_SESSION_ENV_DIR", str(directory))
    monkeypatch.setenv("CC_SESSION_KEY", other_key)
    other_identity = TurnIdentity(
        conversation=ConversationIdentity(platform="qq", chat_kind="group", chat_id="39999"),
        sender_user_id="20002",
        source="cc-connect-sender-envelope",
    )

    with pytest.raises(agent_bridge.TransportAttestationError) as rejected:
        agent_bridge._validate_qq_group_transport_attestation(other_identity, "bound")
    assert rejected.value.code == "qq_transport_attestation_unsafe"


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "weak-mode"])
def test_lock_preoccupation_is_rejected_without_consuming_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    attack: str,
) -> None:
    directory = tmp_path / "session-env"
    _append(directory, actor="20002", text="protected")
    lock_path = _lock_path(directory)
    lock_path.unlink()
    if attack == "symlink":
        outside = tmp_path / "outside.lock"
        outside.write_text("", encoding="utf-8")
        outside.chmod(0o600)
        lock_path.symlink_to(outside)
    else:
        lock_path.write_text("", encoding="utf-8")
        lock_path.chmod(0o600 if attack == "hardlink" else 0o644)
        if attack == "hardlink":
            os.link(lock_path, tmp_path / "lock-hardlink")
    before = _state(directory)
    with pytest.raises(botspec_cli._SessionEnvSecurityError):
        _append(directory, actor="20003", text="must not be appended")
    assert _state(directory) == before
    _bind_runtime_env(monkeypatch, directory)

    with pytest.raises(agent_bridge.TransportAttestationError) as rejected:
        agent_bridge._validate_qq_group_transport_attestation(_turn("20002"), "protected")
    assert rejected.value.code == "qq_transport_attestation_unsafe"
    assert _state(directory) == before


def test_wrapper_identity_reader_takes_shared_lock(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session-env"
    botspec_cli._write_private_session_env(
        directory=directory,
        session_key=SESSION_KEY,
        values=_identity_values(),
    )
    lock_fd = os.open(_lock_path(directory), os.O_RDWR | os.O_NOFOLLOW)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                botspec_cli._read_private_session_env,
                directory=directory,
                session_key=SESSION_KEY,
            )
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.1)
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            assert future.result(timeout=2) == _identity_values()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
