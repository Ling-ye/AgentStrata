import json
from pathlib import Path

from chatcopilot.core.jobs import STATUS_EVENTS_FILENAME, write_job_status


def _events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_job_status_history_records_stage_transitions_not_heartbeats(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "job_test"

    write_job_status(job_dir, "running", "coding", stage="coding", heartbeat_at=1)
    write_job_status(job_dir, "running", "coding", stage="coding", heartbeat_at=2)
    write_job_status(job_dir, "running", "testing", stage="testing", heartbeat_at=3)

    events = _events(job_dir / STATUS_EVENTS_FILENAME)
    assert len(events) == 2
    assert [event["data"]["stage"] for event in events] == ["coding", "testing"]
