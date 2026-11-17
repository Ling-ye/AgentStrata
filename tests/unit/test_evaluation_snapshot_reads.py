"""Exercise private snapshot reads across actual POSIX atomic replacements."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

from chatcopilot.evals.application import controller

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX unlink and no-follow snapshot boundary")


def _private_json(path: Path, value: str, *, mode: int = 0o600) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)


@pytest.fixture
def snapshot_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "observation.json"
    _private_json(path, '{"generation": 0}')
    real_fstat = os.fstat
    descriptors: list[int] = []

    def install(interleave: Callable[[int], None]) -> list[int]:
        def raced_fstat(descriptor: int) -> os.stat_result:
            descriptors.append(descriptor)
            interleave(len(descriptors))
            return real_fstat(descriptor)

        monkeypatch.setattr(controller.os, "fstat", raced_fstat)
        return descriptors

    yield path, install
    for descriptor in set(descriptors):
        with pytest.raises(OSError):
            real_fstat(descriptor)


def test_snapshot_reopens_after_atomic_replacement(snapshot_race) -> None:
    path, install = snapshot_race

    def replace_once(attempt: int) -> None:
        if attempt == 1:
            replacement = path.with_name("replacement.json")
            _private_json(replacement, '{"generation": 1}')
            replacement.replace(path)

    attempts = install(replace_once)
    assert controller._read_json(path, max_bytes=1024) == {"generation": 1}
    assert len(attempts) == 2


def test_snapshot_repeated_replacement_has_bounded_reopens(snapshot_race) -> None:
    path, install = snapshot_race

    def replace_each(attempt: int) -> None:
        replacement = path.with_name(f"replacement-{attempt}.json")
        _private_json(replacement, '{"generation": 1}')
        replacement.replace(path)

    attempts = install(replace_each)
    with pytest.raises(ValueError, match="exactly one hard link"):
        controller._read_json(path)
    assert len(attempts) == 3


@pytest.mark.parametrize("unsafe", ["mode", "hardlink", "symlink"])
def test_snapshot_replacement_still_rejects_unsafe_file(snapshot_race, unsafe: str) -> None:
    path, install = snapshot_race
    sentinel = path.with_name("sentinel.json")
    _private_json(sentinel, '{"protected": true}')

    def replace_once(attempt: int) -> None:
        if attempt != 1:
            return
        replacement = path.with_name("replacement.json")
        if unsafe == "symlink":
            replacement.symlink_to(sentinel)
        else:
            _private_json(replacement, '{"generation": 1}', mode=0o644 if unsafe == "mode" else 0o600)
            if unsafe == "hardlink":
                os.link(replacement, path.with_name("replacement-alias.json"))
        replacement.replace(path)

    attempts = install(replace_once)
    error, match = {
        "mode": (PermissionError, "mode 0600"),
        "hardlink": (ValueError, "exactly one hard link"),
        "symlink": (ValueError, "cannot be opened safely"),
    }[unsafe]
    with pytest.raises(error, match=match):
        controller._read_json(path)
    assert len(attempts) == (1 if unsafe == "symlink" else 2)
    assert sentinel.read_text() == '{"protected": true}'


def test_snapshot_does_not_retry_an_unlinked_file_with_unsafe_mode(snapshot_race) -> None:
    path, install = snapshot_race
    path.chmod(0o644)

    def replace_once(attempt: int) -> None:
        replacement = path.with_name(f"replacement-{attempt}.json")
        _private_json(replacement, '{"generation": 1}')
        replacement.replace(path)

    attempts = install(replace_once)
    with pytest.raises(PermissionError, match="mode 0600"):
        controller._read_json(path)
    assert len(attempts) == 1


def test_snapshot_unlinked_without_replacement_returns_missing(snapshot_race) -> None:
    path, install = snapshot_race

    def unlink_once(attempt: int) -> None:
        assert attempt == 1
        path.unlink()

    attempts = install(unlink_once)
    assert controller._read_json(path) == {}
    assert len(attempts) == 1
