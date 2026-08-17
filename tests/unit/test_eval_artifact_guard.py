"""Focused hostile-filesystem tests for the parent Trial artifact guard."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chatcopilot.evals.artifact_guard import (
    ArtifactIntegrityError,
    ArtifactIntegrityGuard,
)


_AUTHORITY_FILES = (
    "request.json",
    "state.json",
    "result.json",
    "summary.md",
    "progress.jsonl",
)


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _private_file(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _evaluation_root(tmp_path: Path) -> Path:
    root = _private_directory(tmp_path / "evaluation")
    for name in _AUTHORITY_FILES:
        _private_file(root / name, f"{{\"artifact\":\"{name}\"}}\n".encode())
    trial_root = _private_directory(root / "trials")
    _private_file(trial_root / "case-a.json", b'{"trial":"a"}\n')
    return root


def _assert_violation(guard: ArtifactIntegrityGuard) -> None:
    with pytest.raises(ArtifactIntegrityError) as raised:
        guard.verify()
    assert raised.value.code == "artifact_integrity_violation"


def _canonical_cancel(evaluation_id: str) -> bytes:
    return (
        json.dumps(
            {"evaluation_id": evaluation_id, "requested_at": "2026-08-17T00:00:00+00:00"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def test_snapshot_records_all_authority_metadata_and_pins_output(tmp_path: Path) -> None:
    root = _evaluation_root(tmp_path)

    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        snapshot = guard.snapshot
        assert snapshot.root.exists
        assert snapshot.root.kind == "directory"
        assert snapshot.files["request.json"].sha256
        assert snapshot.trials["case-a.json"].kind == "regular"
        assert snapshot.cancel_marker.exists is False
        guard.verify()


def test_allows_user_owned_non_writable_repository_parent(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    root = _evaluation_root(tmp_path)

    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        guard.verify()


def test_rejects_authority_content_mutation(tmp_path: Path) -> None:
    root = _evaluation_root(tmp_path)
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        _private_file(root / "summary.md", b"changed\n")
        _assert_violation(guard)


def test_rejects_same_size_replacement_and_inode_swap(tmp_path: Path) -> None:
    root = _evaluation_root(tmp_path)
    target = root / "request.json"
    original = target.read_bytes()
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        replacement = _private_file(root / "replacement", b"x" * len(original))
        os.replace(replacement, target)
        target.chmod(0o600)
        _assert_violation(guard)


def test_rejects_mode_and_timestamp_mutation(tmp_path: Path) -> None:
    root = _evaluation_root(tmp_path)
    target = root / "state.json"
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        target.chmod(0o644)
        _assert_violation(guard)

    target.chmod(0o600)
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        before = target.stat()
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        _assert_violation(guard)


def test_rejects_symlink_and_hardlink_authority_artifacts(tmp_path: Path) -> None:
    root = _evaluation_root(tmp_path)
    target = root / "result.json"
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        target.unlink()
        target.symlink_to(root / "request.json")
        _assert_violation(guard)

    target.unlink()
    os.link(root / "request.json", target)
    with pytest.raises(ArtifactIntegrityError) as raised:
        ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard")
    assert raised.value.code == "artifact_integrity_violation"


def test_rejects_trials_name_set_and_content_changes(tmp_path: Path) -> None:
    root = _evaluation_root(tmp_path)
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        _private_file(root / "trials" / "case-b.json", b'{"trial":"b"}\n')
        _assert_violation(guard)

    (root / "trials" / "case-b.json").unlink()
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        _private_file(root / "trials" / "case-a.json", b'{"trial":"mutated"}\n')
        _assert_violation(guard)


def test_allows_only_a_new_canonical_cancel_marker_for_same_evaluation(tmp_path: Path) -> None:
    root = _evaluation_root(tmp_path)
    marker = root / ".cancel-requested.json"
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        _private_file(marker, _canonical_cancel("eval-guard"))
        guard.verify()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"evaluation_id":"different"}\n',
        b'{"requested_at":"x", "evaluation_id":"eval-guard"}\n',
        b'{"evaluation_id":"eval-guard","value":NaN}\n',
    ],
)
def test_rejects_malformed_or_noncanonical_new_cancel_marker(tmp_path: Path, payload: bytes) -> None:
    root = _evaluation_root(tmp_path)
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        _private_file(root / ".cancel-requested.json", payload)
        _assert_violation(guard)


def test_rejects_existing_cancel_marker_replacement_or_deletion(tmp_path: Path) -> None:
    root = _evaluation_root(tmp_path)
    marker = _private_file(root / ".cancel-requested.json", _canonical_cancel("eval-guard"))
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        marker.unlink()
        _assert_violation(guard)


def test_rejects_oversized_cancel_marker(tmp_path: Path) -> None:
    root = _evaluation_root(tmp_path)
    marker = root / ".cancel-requested.json"
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        _private_file(marker, b"{" + b"x" * (64 * 1024) + b"}")
        _assert_violation(guard)


def test_optional_claim_path_is_frozen(tmp_path: Path) -> None:
    root = _evaluation_root(tmp_path)
    claims = _private_directory(tmp_path / "claims")
    claim = _private_file(claims / "bot.claim", b'{"evaluation_id":"eval-guard"}\n')
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard", claim_path=claim) as guard:
        _private_file(claim, b'{"evaluation_id":"changed"}\n')
        _assert_violation(guard)


def test_allows_other_bot_siblings_in_shared_managed_root(tmp_path: Path) -> None:
    managed_root = _private_directory(tmp_path / "evaluations")
    root = _evaluation_root(managed_root)
    claim = _private_file(
        managed_root / ".active-bot-a.json",
        b'{"evaluation_id":"eval-guard"}\n',
    )

    with ArtifactIntegrityGuard.capture(
        root,
        evaluation_id="eval-guard",
        claim_path=claim,
    ) as guard:
        _private_directory(managed_root / "eval-b")
        _private_file(
            managed_root / ".active-bot-b.json",
            b'{"evaluation_id":"eval-b"}\n',
        )
        guard.verify()


def test_rejects_replaced_shared_managed_root(tmp_path: Path) -> None:
    managed_root = _private_directory(tmp_path / "evaluations")
    root = _evaluation_root(managed_root)
    claim = _private_file(
        managed_root / ".active-bot-a.json",
        b'{"evaluation_id":"eval-guard"}\n',
    )

    with ArtifactIntegrityGuard.capture(
        root,
        evaluation_id="eval-guard",
        claim_path=claim,
    ) as guard:
        managed_root.rename(tmp_path / "old-evaluations")
        _private_directory(managed_root)
        _assert_violation(guard)


def test_rejects_replaced_output_directory_even_though_pinned_fd_survives(tmp_path: Path) -> None:
    root = _evaluation_root(tmp_path)
    with ArtifactIntegrityGuard.capture(root, evaluation_id="eval-guard") as guard:
        moved = tmp_path / "old-evaluation"
        root.rename(moved)
        _evaluation_root(tmp_path)
        _assert_violation(guard)
