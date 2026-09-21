from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import hashlib

import pytest
import yaml

from chatcopilot.gateway.observation_store import ObservationStore
from chatcopilot.gateway.state_store import GatewayStateStore
from chatcopilot.runtime_cutover import apply_inventory, check_inventory, verify_inventory


def _fixture(tmp_path: Path, *, active_worker: bool = False) -> Path:
    bot = tmp_path / "bot"
    state = tmp_path / "state"
    workspace = tmp_path / "workspaces"
    evaluations = tmp_path / "evaluations"
    for path in (bot, state, workspace, evaluations):
        path.mkdir(mode=0o700)
    (bot / "identity.md").write_text("fixture", encoding="utf-8")
    (bot / "bot.yaml").write_text(
        "\n".join(
            (
                "id: fixture",
                "agents:",
                "  runtime: native",
                "llm:",
                "  chat:",
                "    env_prefix: CHATCOPILOT_FIXTURE",
                "prompts:",
                "  schema_version: 2",
                "  identity: identity.md",
                "  response_style: identity.md",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    private_env = bot / "local.env"
    private_env.write_text("CHATCOPILOT_FIXTURE_API_KEY=fixture\n", encoding="utf-8")
    private_env.chmod(0o600)
    state_store = GatewayStateStore(state)
    lease = state_store.acquire_instance_lease()
    lease.close()
    observation = ObservationStore(state, writable=True)
    observation.put_configuration({"runtime_id": "native", "model": "legacy"})
    evaluation = evaluations / "eval-old"
    evaluation.mkdir(mode=0o700)
    (evaluation / "result.json").write_text(
        json.dumps({"schema_version": 2, "trials": []}), encoding="utf-8"
    )
    transcript = workspace / "p2p_fixture" / "transcripts"
    transcript.mkdir(parents=True)
    (transcript / "turn.jsonl").write_text("legacy transcript", encoding="utf-8")
    binding = workspace / "p2p_fixture" / ".runtime-sessions"
    binding.mkdir()
    (binding / "legacy.session.json").write_text("{}", encoding="utf-8")
    if active_worker:
        job = workspace / "p2p_fixture" / "jobs" / "job_fixture"
        job.mkdir(parents=True)
        (job / "status.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
    inventory = tmp_path / "runtime-cutover.yaml"
    inventory.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "instances": [
                    {
                        "instance_id": "fixture",
                        "bot_spec": str((bot / "bot.yaml").resolve()),
                        "private_env": str(private_env.resolve()),
                        "gateway_state_root": str(state.resolve()),
                        "workspace_root": str(workspace.resolve()),
                        "evaluation_root": str(evaluations.resolve()),
                        "expected_runtime_id": "native",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return inventory


def test_inventory_cutover_archives_old_evidence_and_strict_loads_new_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = _fixture(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    plan = check_inventory(inventory)
    after = {
        path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before

    result = apply_inventory(inventory, plan_digest=plan["plan_digest"])
    receipt = Path(result["receipt"])
    monkeypatch.setattr(
        "chatcopilot.runtime_cutover._controlled_gateway_replay",
        lambda _instance: {
            "production_delivery": False,
            "gateway_replay": "passed",
            "gateway_layers": ["gateway"],
        },
    )
    verified = verify_inventory(inventory, receipt)

    assert verified["verified"] is True
    assert verified["instances"][0]["production_delivery"] is False
    record = result["instances"][0]
    archive = Path(record["archive"])
    assert (archive / "gateway" / "observability" / "index.sqlite3").is_file()
    assert (archive / "evaluations" / "eval-old" / "result.json").is_file()
    assert (archive / "workspace" / "p2p_fixture" / "transcripts" / "turn.jsonl").is_file()
    assert not (tmp_path / "workspaces" / "p2p_fixture" / ".runtime-sessions").exists()
    assert ObservationStore(tmp_path / "state").meta("missing") is None
    with sqlite3.connect(tmp_path / "state" / "observability" / "index.sqlite3") as connection:
        assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
        assert "backend" not in {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        assert "runtime_id" in {row[1] for row in connection.execute("PRAGMA table_info(events)")}
    assert json.loads((tmp_path / "workspaces" / ".agent-runtime.json").read_text())["runtime_id"] == "native"
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_cutover_rejects_plan_drift_before_writing(tmp_path: Path) -> None:
    inventory = _fixture(tmp_path)
    plan = check_inventory(inventory)
    bot = tmp_path / "bot" / "bot.yaml"
    bot.write_text(bot.read_text() + "# changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="plan digest"):
        apply_inventory(inventory, plan_digest=plan["plan_digest"])

    assert not list((tmp_path / "state").glob("runtime-cutover-*"))


def test_cutover_check_rejects_active_worker(tmp_path: Path) -> None:
    inventory = _fixture(tmp_path, active_worker=True)

    with pytest.raises(ValueError, match="unfinished worker"):
        check_inventory(inventory)
