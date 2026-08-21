from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatcopilot.core.ingress_receipts import (
    INGRESS_RECEIPT_TTL_NS,
    IngressReceiptError,
    append_ingress_receipt,
    consume_ingress_receipt,
    receipt_root_from_env,
)
from chatcopilot.middleware.acp.turn_orchestrator import AcpTurnOrchestrator
from chatcopilot.middleware.acp.turn_pipeline import TurnContext


_DECISION = {
    "code": "group_mention_matched",
    "outcome": "forward",
    "user_allowed": True,
    "group_allowed": False,
    "mention_required": True,
    "mention_satisfied": True,
}


def _append(root: Path, *, text: str = "你是谁", now_ns: int = 10_000_000_000) -> None:
    append_ingress_receipt(
        root,
        platform="qq",
        chat_kind="group",
        chat_id="group-private-id",
        actor_id="actor-private-id",
        content=text,
        message_id=12345,
        segment_count=2,
        decision=_DECISION,
        now_ns=now_ns,
    )


def _consume(root: Path, *, text: str = "你是谁", now_ns: int = 11_000_000_000):
    return consume_ingress_receipt(
        root,
        platform="qq",
        chat_kind="group",
        chat_id="group-private-id",
        actor_id="actor-private-id",
        content=text,
        now_ns=now_ns,
    )


def test_ingress_receipt_matches_once_and_persists_only_digests(tmp_path: Path) -> None:
    root = tmp_path / "private" / "ingress-receipts"
    root.parent.mkdir(mode=0o700)

    _append(root)
    state_text = (root / "receipts.json").read_text(encoding="utf-8")

    for private_value in ("group-private-id", "actor-private-id", "你是谁", "12345"):
        assert private_value not in state_text
    state = json.loads(state_text)
    assert state["receipts"][0]["decision"]["authoritative"] is False
    assert (root.stat().st_mode & 0o777) == 0o700
    assert ((root / "receipts.json").stat().st_mode & 0o777) == 0o600

    matched = _consume(root)
    replay = _consume(root)

    assert matched.status == "matched"
    assert matched.receipt is not None
    assert replay.status == "missing"


def test_duplicate_ingress_receipts_are_left_ambiguous(tmp_path: Path) -> None:
    root = tmp_path / "private" / "ingress-receipts"
    root.parent.mkdir(mode=0o700)
    _append(root, now_ns=10_000_000_000)
    _append(root, now_ns=10_100_000_000)

    result = _consume(root, now_ns=11_000_000_000)

    assert result.status == "ambiguous"
    assert result.reason == "duplicate_candidates"
    assert len(json.loads((root / "receipts.json").read_text())["receipts"]) == 2


def test_stale_receipts_are_removed_without_matching(tmp_path: Path) -> None:
    root = tmp_path / "private" / "ingress-receipts"
    root.parent.mkdir(mode=0o700)
    _append(root, now_ns=1_000_000_000)

    result = _consume(
        root,
        now_ns=1_000_000_000 + INGRESS_RECEIPT_TTL_NS + 1,
    )

    assert result.status == "missing"
    assert json.loads((root / "receipts.json").read_text())["receipts"] == []


def test_ingress_receipt_rejects_symlink_and_hardlinked_state(tmp_path: Path) -> None:
    real_root = tmp_path / "private" / "real"
    real_root.parent.mkdir(mode=0o700)
    real_root.mkdir(mode=0o700)
    symlink = tmp_path / "private" / "alias"
    symlink.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(IngressReceiptError):
        _append(symlink)

    root = tmp_path / "private" / "ingress-receipts"
    _append(root)
    os.link(root / "receipts.json", tmp_path / "private" / "state-hardlink.json")
    with pytest.raises(IngressReceiptError):
        _consume(root)


def test_receipt_root_requires_explicit_absolute_private_location(tmp_path: Path) -> None:
    assert receipt_root_from_env({}) is None
    assert receipt_root_from_env({"CHATCOPILOT_CC_HOME": str(tmp_path)}) == (
        tmp_path / "ingress-receipts"
    )
    with pytest.raises(IngressReceiptError):
        receipt_root_from_env({"CHATCOPILOT_INGRESS_RECEIPT_DIR": "relative/path"})


def test_acp_correlates_receipt_as_non_authoritative_flow_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "private" / "ingress-receipts"
    root.parent.mkdir(mode=0o700)
    append_ingress_receipt(
        root,
        platform="qq",
        chat_kind="p2p",
        chat_id="actor-private-id",
        actor_id="actor-private-id",
        content="你是谁",
        decision={**_DECISION, "code": "private_user_allowed"},
        now_ns=10_000_000_000,
    )
    monkeypatch.setenv("CHATCOPILOT_INGRESS_RECEIPT_DIR", str(root))
    monkeypatch.setattr(
        "chatcopilot.core.ingress_receipts.time.time_ns",
        lambda: 11_000_000_000,
    )
    recorded: list[tuple[str, dict]] = []
    recorder = SimpleNamespace(
        task_id="task_receipt",
        record_event=lambda event_type, payload: recorded.append((event_type, payload)),
    )
    turn = TurnContext(
        session_id="session",
        session=SimpleNamespace(),
        user_text="你是谁",
        message_id=None,
        turn_task=recorder,
    )
    orchestrator = object.__new__(AcpTurnOrchestrator)
    orchestrator._platform_type = "qq"

    orchestrator._record_ingress_receipt(
        turn,
        workspace=SimpleNamespace(
            chat_kind="p2p",
            chat_id="actor-private-id",
            user_id="actor-private-id",
        ),
        turn_identity=None,
    )

    assert [item[1]["kind"] for item in recorded] == [
        "transport.onebot_message_received",
        "gateway.access_decision",
    ]
    assert all(item[0] == "flow_transition" for item in recorded)
    payload = recorded[1][1]
    assert payload["kind"] == "gateway.access_decision"
    assert payload["evidence_level"] == "correlated"
    assert payload["decision"] == {
        "code": "private_user_allowed",
        "outcome": "forward",
        "allowed": True,
        "authoritative": False,
    }
    serialized = json.dumps(recorded, ensure_ascii=False)
    assert "actor-private-id" not in serialized
    assert "你是谁" not in serialized


def test_missing_receipt_records_gap_without_raising(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "private" / "missing-receipts"
    root.parent.mkdir(mode=0o700)
    monkeypatch.setenv("CHATCOPILOT_INGRESS_RECEIPT_DIR", str(root))
    recorded: list[tuple[str, dict]] = []
    turn = TurnContext(
        session_id="session",
        session=SimpleNamespace(),
        user_text="still authorized elsewhere",
        message_id=None,
        turn_task=SimpleNamespace(
            task_id="task_missing_receipt",
            record_event=lambda event_type, payload: recorded.append((event_type, payload)),
        ),
    )
    orchestrator = object.__new__(AcpTurnOrchestrator)
    orchestrator._platform_type = "qq"

    orchestrator._record_ingress_receipt(
        turn,
        workspace=SimpleNamespace(chat_kind="p2p", chat_id="actor", user_id="actor"),
        turn_identity=None,
    )

    assert recorded[0][1]["evidence_level"] == "missing"
    assert recorded[0][1]["status"] == "unknown"
    assert recorded[0][1]["decision"]["authoritative"] is False
