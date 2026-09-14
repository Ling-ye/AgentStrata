from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.progress import BODY_BYTES, TAIL_BYTES, read_progress
from chatcopilot.harness.store import HarnessStore
from console.backend.routes.harness import router

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Harness worker uses Linux private storage")


@pytest.fixture
def context(tmp_path):
    root = tmp_path / "harness"
    root.mkdir(mode=0o700)
    task = {"task_id": "repair-progress-example", "stage": "coding", "status": "running", "current_attempt": 2}
    attempts = [{"number": 1, "status": "rejected"}, {"number": 2, "status": "coding"}]
    return root, task, attempts


def write_log(context, relative, items):
    root, task, _ = context
    path = root / "jobs" / task["task_id"] / relative / "public-events.jsonl"
    for parent in reversed(path.parents):
        if parent.is_relative_to(root) and not parent.exists():
            parent.mkdir(mode=0o700)
    path.write_bytes(b"".join(json.dumps(item).encode() + b"\n" for item in items))
    path.chmod(0o600)
    return path


def message(text):
    return {"type": "agent_message", "text": text}


def test_missing_log_does_not_create_directories(context):
    root, _, _ = context
    assert read_progress(*context) == {
        "state": "empty", "source": None, "updated_at": None,
        "events": [], "truncated": False, "message": None,
    }
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(("stage", "relative", "kind", "number"), [
    ("prepare_reproducer", "reproducer", "prepare", None),
    ("auto_correcting", "reproducer/revision-3", "prepare", 3),
    ("coding", "attempt-2", "coding", 2),
    ("review", "attempt-2/review", "review", 2),
])
def test_selects_current_stage_before_newer_other_log(context, stage, relative, kind, number):
    _, task, _ = context
    task["stage"] = stage
    if number == 3:
        task["preparation_revisions"] = [{"revision": 1}, {"revision": 3}]
    path = write_log(context, relative, [message("current")])
    os.utime(path, (100, 100))
    write_log(context, "attempt-1", [message("previous")])
    progress = read_progress(*context)
    assert progress["source"] == {"id": f"{kind}-{number}" if number else kind,
                                  "kind": kind, "number": number, "current": True}
    assert progress["updated_at"] == 100
    assert progress["events"][0]["text"] == "current"


@pytest.mark.parametrize("status", ["running", "cancel_requested", "blocked", "interrupted", "cancelled", "fixed"])
def test_verification_or_terminal_keeps_last_output_as_other_stage(context, status):
    _, task, _ = context
    task.update(stage="verify-2", status=status)
    write_log(context, "attempt-2", [message("candidate generated")])
    progress = read_progress(*context)
    assert progress["source"]["current"] is False
    assert progress["source"]["number"] == 2
    assert task["status"] == status


def test_falls_back_when_current_stage_has_not_created_log(context):
    write_log(context, "attempt-1", [message("previous candidate")])
    progress = read_progress(*context)
    assert progress["source"]["current"] is False
    assert progress["source"]["number"] == 1


def test_shared_jobs_container_can_inherit_umask_inside_private_root(context):
    root, _, _ = context
    write_log(context, "attempt-2", [message("current")])
    (root / "jobs").chmod(0o775)
    assert read_progress(*context)["events"][0]["text"] == "current"
    root.chmod(0o755)
    with pytest.raises(HarnessError):
        read_progress(*context)


def test_empty_current_file_does_not_present_previous_output_as_current(context):
    write_log(context, "attempt-1", [message("previous")])
    write_log(context, "attempt-2", [])
    progress = read_progress(*context)
    assert progress["state"] == "empty"
    assert progress["source"]["current"] is True
    assert progress["events"] == []


def test_append_is_readable_before_writer_exits_and_partial_line_waits(context):
    path = write_log(context, "attempt-2", [])
    script = """
import json, sys
with open(sys.argv[1], 'ab', buffering=0) as stream:
    stream.write((json.dumps({'type': 'agent_message', 'text': 'first'}) + '\\n').encode())
    print('first', flush=True)
    sys.stdin.readline()
    stream.write(b'{"type":"agent_message","text":"second"}')
    print('partial', flush=True)
    sys.stdin.readline()
    stream.write(b'\\n')
    print('complete', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen([sys.executable, "-u", "-c", script, str(path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "first"
        first = read_progress(*context)
        assert [event["text"] for event in first["events"]] == ["first"]
        process.stdin.write("next\n")
        process.stdin.flush()
        assert process.stdout.readline().strip() == "partial"
        partial = read_progress(*context)
        assert partial["events"] == first["events"]
        process.stdin.write("next\n")
        process.stdin.flush()
        assert process.stdout.readline().strip() == "complete"
        complete = read_progress(*context)
        assert [event["text"] for event in complete["events"]] == ["first", "second"]
        assert complete["events"][0]["id"] == first["events"][0]["id"]
        assert process.poll() is None
    finally:
        process.communicate("stop\n", timeout=5)
    assert process.returncode == 0


def test_tail_limits_keep_order_and_unicode_bytes(context, monkeypatch):
    path = write_log(context, "attempt-2", [message("old" * TAIL_BYTES)] + [message(str(i)) for i in range(30)])
    original = os.read
    reads = []

    def bounded(fd, count):
        reads.append(count)
        return original(fd, count)

    monkeypatch.setattr(os, "read", bounded)
    progress = read_progress(*context)
    assert reads == [TAIL_BYTES]
    assert progress["truncated"] is True
    assert [event["text"] for event in progress["events"]] == [str(i) for i in range(10, 30)]
    path.write_text(json.dumps(message("中" * BODY_BYTES)) + "\n")
    event = read_progress(*context)["events"][0]
    assert event["truncated"] is True
    assert 0 < len(event["text"].encode()) <= BODY_BYTES
    assert "\ufffd" not in event["text"]


def test_untrusted_or_malformed_events_are_not_returned_raw(context):
    secret = "sk-" + "progressfixture123456789012345678901234567890"
    path = write_log(context, "attempt-2", [
        {"type": "reasoning", "text": "private thought"},
        {"type": "agent_message", "text": {"private": "not a message"}},
        {"type": "command_execution", "command": "echo example", "exit_code": "invalid"},
        {"type": "command_execution", "command": "echo example", "aggregated_output": f"api_key={secret}",
         "exit_code": 0, "reasoning": "private thought", "auth": secret},
    ])
    with path.open("ab") as stream:
        stream.write(b"not-json\n[]\n")
    progress = read_progress(*context)
    assert progress["state"] == "partial"
    assert progress["message"]
    assert len(progress["events"]) == 1
    assert progress["events"][0]["exit_code"] == 0
    serialized = json.dumps(progress)
    assert secret not in serialized
    assert "private thought" not in serialized
    assert "not-json" not in serialized


@pytest.mark.parametrize("unsafe", ["file_symlink", "directory_symlink", "hardlink", "mode", "directory_mode", "fifo"])
def test_rejects_unsafe_files_without_reading_other_task(context, unsafe):
    root, task, _ = context
    other = (root, {**task, "task_id": "repair-other"}, [])
    target = write_log(other, "attempt-2", [message("other task")])
    path = write_log(context, "attempt-2", [])
    if unsafe == "file_symlink":
        path.unlink()
        path.symlink_to(target)
    elif unsafe == "directory_symlink":
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(target.parent, target_is_directory=True)
    elif unsafe == "hardlink":
        path.unlink()
        os.link(target, path)
    elif unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "directory_mode":
        path.parent.chmod(0o755)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    with pytest.raises(HarnessError, match="暂不可读取") as error:
        read_progress(*context)
    assert error.value.code == "progress_unavailable"
    assert "other task" not in str(error.value)


def test_unknown_task_and_log_failure_do_not_change_database(context):
    root, task, attempts = context
    controller = HarnessController.__new__(HarnessController)
    controller.store = HarnessStore(root)
    controller.store.create({**task, "request_key": "request-example", "match_key": "match-example",
                             "context_key": "context-example", "active_key": "active-example"})
    controller.store.update(task["task_id"], status="running", stage="coding", dispatch_state="scheduled")
    for attempt in attempts:
        controller.store.save_attempt(task["task_id"], attempt["number"], attempt)
    before = controller.store.get(task["task_id"])
    path = write_log(context, "attempt-2", [message("in progress")])
    assert controller.progress(task["task_id"])["events"][0]["text"] == "in progress"
    path.chmod(0o644)
    with pytest.raises(HarnessError):
        controller.progress(task["task_id"])
    with pytest.raises(HarnessError) as missing:
        controller.progress("repair-other")
    assert missing.value.code == "not_found"
    assert controller.store.get(task["task_id"]) == before


@pytest.mark.parametrize(("code", "status"), [(None, 200), ("not_found", 404), ("progress_unavailable", 409)])
def test_route_uses_public_controller_and_never_caches(context, code, status):
    app = FastAPI()
    app.include_router(router)
    read = Mock(return_value=read_progress(*context), side_effect=HarnessError(code, "unavailable") if code else None)
    app.state.harness = SimpleNamespace(progress=read)
    with TestClient(app) as client:
        response = client.get("/api/harness/tasks/repair-example/progress")
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    read.assert_called_once_with("repair-example")


def test_governance_log_is_current_only_during_its_actual_phase():
    from chatcopilot.harness.progress import _logs
    task = {'stage': 'verify', 'source': {'governance_version': 2}, 'current_source': 'attempt-1',
            'progress_sources': [{'id': 'attempt-1', 'path': 'attempt-1/public-events.jsonl', 'kind': 'coding', 'number': 1}]}
    assert list(_logs(task, []))[0][1]['current'] is False
    task['stage'] = 'coding'
    assert list(_logs(task, []))[0][1]['current'] is True
    task['stage'] = 'repository_baseline'
    assert list(_logs(task, []))[0][1]['current'] is False
