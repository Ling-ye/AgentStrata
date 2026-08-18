import json
import os
import stat
from pathlib import Path

from chatcopilot.core.jobs import (
    NOTIFICATION_FILENAME,
    REQUEST_FILENAME,
    RESULT_FILENAME,
    STATUS_FILENAME,
    STATUS_EVENTS_FILENAME,
    BackgroundJob,
    read_job_notification,
    read_job_result,
    read_job_status,
    read_json_file,
    request_job_cancel,
    write_job_status,
    write_json_atomic,
)
from chatcopilot.middleware.runtime.jobs.notification import (
    read_json_file as read_notification_json_file,
    write_json_atomic as write_notification_json_atomic,
)


def _events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _job(job_dir: Path) -> BackgroundJob:
    return BackgroundJob(
        job_id=job_dir.name,
        tool_name="test_tool",
        execution_policy="background",
        job_dir=job_dir,
        request_path=job_dir / REQUEST_FILENAME,
        result_path=job_dir / RESULT_FILENAME,
    )


def test_job_json_readers_reject_links_and_writable_artifacts(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "job_private_readers"
    job_dir.mkdir(parents=True, mode=0o700)
    victim = tmp_path / "private.json"
    victim.write_text('{"secret":"must-not-be-read"}', encoding="utf-8")

    readers = {
        STATUS_FILENAME: lambda: read_job_status(_job(job_dir)),
        NOTIFICATION_FILENAME: lambda: read_job_notification(_job(job_dir)),
        REQUEST_FILENAME: lambda: read_json_file(job_dir / REQUEST_FILENAME),
    }
    for name, reader in readers.items():
        artifact = job_dir / name
        artifact.symlink_to(victim)
        assert reader() is None
        artifact.unlink()

        os.link(victim, artifact)
        assert reader() is None
        artifact.unlink()

        artifact.write_text('{"trusted":true}', encoding="utf-8")
        artifact.chmod(0o666)
        assert reader() is None
        artifact.unlink()

    result_path = job_dir / RESULT_FILENAME
    assert read_job_result(_job(job_dir)) is None
    result_path.symlink_to(victim)
    result = read_job_result(_job(job_dir))
    assert result is not None
    assert result["error_code"] == "result_artifact_unsafe"
    assert "must-not-be-read" not in json.dumps(result)


def test_notification_compat_readers_and_writers_use_private_job_boundary(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "jobs" / "job_notification_boundary"
    job_dir.mkdir(parents=True, mode=0o700)
    victim = tmp_path / "notification-victim.json"
    victim.write_text('{"secret":"keep"}', encoding="utf-8")
    notification = job_dir / NOTIFICATION_FILENAME
    notification.symlink_to(victim)

    assert read_notification_json_file(notification) is None
    try:
        write_notification_json_atomic(notification, {"delivery": "delivered"})
    except OSError:
        pass
    else:
        raise AssertionError("notification writer must reject a symlink artifact")
    assert victim.read_text(encoding="utf-8") == '{"secret":"keep"}'


def test_job_reader_rejects_symlinked_job_directory_ancestor(tmp_path: Path) -> None:
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    external = tmp_path / "external-private-job"
    external.mkdir()
    (external / RESULT_FILENAME).write_text(
        '{"ok":true,"summary":"PRIVATE_RESULT_CONTENT"}',
        encoding="utf-8",
    )
    job_dir = jobs_root / "job_symlinked_ancestor"
    job_dir.symlink_to(external, target_is_directory=True)

    result = read_job_result(_job(job_dir))

    assert result is not None
    assert result["error_code"] == "result_artifact_unsafe"
    assert "PRIVATE_RESULT_CONTENT" not in json.dumps(result)


def test_oversized_job_result_returns_bounded_terminal_manifest(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "job_oversized_result"
    job_dir.mkdir(parents=True, mode=0o700)
    result_path = job_dir / RESULT_FILENAME
    write_notification_json_atomic(
        result_path,
        {
            "job_id": job_dir.name,
            "tool_name": "test_tool",
            "ok": True,
            "summary": "x" * (8 * 1024 * 1024),
        },
    )

    result = read_job_result(_job(job_dir))

    assert result is not None
    assert result["ok"] is False
    assert result["stage"] == "failed"
    assert result["error_code"] == "result_artifact_too_large"
    assert result["payload_truncated"] is True
    assert result["details"]["artifact_size_bytes"] > 8 * 1024 * 1024
    assert len(json.dumps(result)) < 2048
    assert result_path.stat().st_size < 2048


def test_shallow_wide_job_result_is_rejected_before_tree_materialization(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "jobs" / "job_shallow_wide_result"
    job_dir.mkdir(parents=True, mode=0o700)
    result_path = job_dir / RESULT_FILENAME
    result_path.write_bytes(
        b'{"job_id":"job_shallow_wide_result","items":['
        + b",".join([b"0"] * 200_010)
        + b"]}"
    )

    result = read_job_result(_job(job_dir))

    assert result is not None
    assert result["ok"] is False
    assert result["error_code"] == "result_artifact_too_large"
    assert result["payload_truncated"] is True


def test_generic_job_json_compat_reader_preflights_shallow_wide_json(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "compat.json"
    artifact.write_bytes(
        b'{"items":[' + b",".join([b"0"] * 200_010) + b"]}"
    )

    assert read_json_file(artifact) is None


def test_non_object_job_result_returns_terminal_integrity_manifest(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "job_non_object_result"
    job_dir.mkdir(parents=True, mode=0o700)
    (job_dir / RESULT_FILENAME).write_text('["not", "an", "object"]', encoding="utf-8")

    result = read_job_result(_job(job_dir))

    assert result is not None
    assert result["ok"] is False
    assert result["error_code"] == "result_artifact_unsafe"


def test_job_status_history_records_stage_transitions_not_heartbeats(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "job_test"

    write_job_status(job_dir, "running", "coding", stage="coding", heartbeat_at=1)
    write_job_status(job_dir, "running", "coding", stage="coding", heartbeat_at=2)
    write_job_status(job_dir, "running", "testing", stage="testing", heartbeat_at=3)

    events = _events(job_dir / STATUS_EVENTS_FILENAME)
    assert len(events) == 2
    assert [event["data"]["stage"] for event in events] == ["coding", "testing"]


def test_job_status_events_are_private_redacted_and_bounded(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "job_private_event"
    encoded_value = "".join(("dXNl", "cjpw", "YXNz", "d29y", "ZA=="))

    write_job_status(
        job_dir,
        "running",
        "Authorization: " + "Basic " + encoded_value,
        stage="coding",
        details={"output": "x" * (600 * 1024)},
    )

    status_text = (job_dir / "status.json").read_text(encoding="utf-8")
    event_path = job_dir / STATUS_EVENTS_FILENAME
    event_line = event_path.read_bytes()
    event = json.loads(event_line)
    assert encoded_value not in status_text
    assert encoded_value.encode() not in event_line
    assert len(event_line) < 64 * 1024
    assert event["payload_truncated"] is True
    assert event["sanitization"]["redacted_before_persistence"] is True
    if os.name == "posix":
        assert stat.S_IMODE(event_path.stat().st_mode) == 0o600


def test_job_status_event_writer_only_migrates_read_only_legacy_permissions(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        return
    safe_job_dir = tmp_path / "jobs" / "job_safe_legacy_event"
    write_job_status(safe_job_dir, "running", "coding", stage="coding")
    safe_event_path = safe_job_dir / STATUS_EVENTS_FILENAME
    safe_event_path.chmod(0o644)

    write_job_status(safe_job_dir, "running", "testing", stage="testing")

    assert stat.S_IMODE(safe_event_path.stat().st_mode) == 0o600
    assert len(_events(safe_event_path)) == 2

    unsafe_job_dir = tmp_path / "jobs" / "job_unsafe_legacy_event"
    write_job_status(unsafe_job_dir, "running", "coding", stage="coding")
    unsafe_event_path = unsafe_job_dir / STATUS_EVENTS_FILENAME
    unsafe_event_path.chmod(0o666)
    original = unsafe_event_path.read_bytes()

    payload = write_job_status(
        unsafe_job_dir,
        "running",
        "testing",
        stage="testing",
    )

    assert payload["stage"] == "testing"
    assert unsafe_event_path.read_bytes() == original
    assert stat.S_IMODE(unsafe_event_path.stat().st_mode) == 0o666


def test_job_status_redaction_bounds_deeply_nested_details(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "job_deep_details"
    nested: list[object] = []
    cursor = nested
    for _ in range(2000):
        child: list[object] = []
        cursor.append(child)
        cursor = child

    payload = write_job_status(
        job_dir,
        "running",
        "working",
        stage="coding",
        details={"nested": nested},
    )

    persisted = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert "[TRUNCATED:DEPTH]" in json.dumps(payload)
    assert persisted == payload


def test_job_event_sink_failure_does_not_break_cancellation_state(tmp_path: Path) -> None:
    if not hasattr(os, "link"):
        return
    job_dir = tmp_path / "jobs" / "job_event_sink_failure"
    job_dir.mkdir(parents=True)
    write_json_atomic(
        job_dir / "status.json",
        {"status": "running", "stage": "running", "message": "running"},
    )
    victim = tmp_path / "event-victim"
    victim.write_text("KEEP-EVENT", encoding="utf-8")
    os.link(victim, job_dir / STATUS_EVENTS_FILENAME)
    job = BackgroundJob(
        job_id=job_dir.name,
        tool_name="test_tool",
        execution_policy="background",
        job_dir=job_dir,
        request_path=job_dir / "request.json",
        result_path=job_dir / "result.json",
    )

    assert request_job_cancel(job, requested_by="owner") is True

    assert job.cancellation_path.is_file()
    assert read_job_status(job)["status"] == "cancel_requested"
    assert victim.read_text(encoding="utf-8") == "KEEP-EVENT"


def test_job_status_writer_rejects_symlinked_job_path_before_any_write(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    jobs_root = workspace_root / "jobs"
    jobs_root.mkdir(parents=True)
    external = tmp_path / "external-job"
    external.mkdir(mode=0o755)
    target_mode = stat.S_IMODE(external.stat().st_mode)
    (jobs_root / "job_symlink").symlink_to(external, target_is_directory=True)

    try:
        write_job_status(
            jobs_root / "job_symlink",
            "running",
            "must not escape",
            stage="coding",
        )
    except OSError:
        pass
    else:
        raise AssertionError("symlinked job directory must be rejected")

    assert stat.S_IMODE(external.stat().st_mode) == target_mode
    assert list(external.iterdir()) == []

    ancestor_workspace = tmp_path / "ancestor-workspace"
    ancestor_workspace.mkdir()
    external_jobs = tmp_path / "external-jobs"
    external_jobs.mkdir(mode=0o755)
    ancestor_mode = stat.S_IMODE(external_jobs.stat().st_mode)
    (ancestor_workspace / "jobs").symlink_to(
        external_jobs,
        target_is_directory=True,
    )

    try:
        write_job_status(
            ancestor_workspace / "jobs" / "job_ancestor_symlink",
            "running",
            "must not escape through ancestor",
            stage="coding",
        )
    except OSError:
        pass
    else:
        raise AssertionError("symlinked jobs ancestor must be rejected")

    assert stat.S_IMODE(external_jobs.stat().st_mode) == ancestor_mode
    assert list(external_jobs.iterdir()) == []
