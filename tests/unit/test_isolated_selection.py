"""Atomic selector behavior uses real private SQLite and no runtime execution."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.storage_files import tighten
from chatcopilot.deployment.isolated_selection import IsolatedSelection
from chatcopilot.deployment.runtime_acceptance import canonical, digest
from tests.unit.test_runtime_acceptance import h


@pytest.fixture
def selector(tmp_path):
    path = private_directory(tmp_path/"private")/"runtime-selection.sqlite"
    return IsolatedSelection(path, h("lab-anchor")), path


def desired(store, generation=1, version="a"):
    return {**store.read(), "generation":generation, "acceptance_sha256":h("receipt-"+version), "intent_sha256":h("intent-"+version)}


def test_atomic_selector_reopens_and_preserves_frozen_before_history(selector):
    store, path = selector
    before = store.read()
    key = digest(canonical(before))
    after = desired(store)
    store.compare_and_select(key, after)
    assert IsolatedSelection(path, h("lab-anchor")).read() == after
    assert store.historical(key) == before
    with pytest.raises(InstallError, match="changed before"):
        store.compare_and_select(key, desired(store, 2, "b"))
    assert store.read() == after


@pytest.mark.parametrize("mutation", [
    lambda v:v.update(extra=True), lambda v:v.update(anchor_sha256=h("other")), lambda v:v.update(generation=True),
    lambda v:v.update(generation=-1), lambda v:v.update(intent_sha256=None), lambda v:v.update(acceptance_sha256="../escape"),
    lambda v:v.update(acceptance_sha256=None),
], ids=["extra", "anchor", "boolean-generation", "negative-generation", "missing-intent", "unsafe-selection", "false-empty"])
def test_unsafe_or_foreign_selection_is_rejected_without_changes(selector, mutation):
    store, _path = selector
    before = store.read()
    value = desired(store)
    mutation(value)
    with pytest.raises(InstallError):
        store.compare_and_select(digest(canonical(before)), value)
    assert store.read() == before


def test_transaction_failure_does_not_leave_partial_selector_or_history(selector, monkeypatch):
    store, _path = selector
    before = store.read()
    original = store.db.connect
    @contextmanager
    def fail_commit(*, write=False):
        with original(write=write) as connection:
            yield connection
            if write:
                raise RuntimeError("injected before SQLite commit")
    monkeypatch.setattr(store.db, "connect", fail_commit)
    with pytest.raises(RuntimeError):
        store.compare_and_select(digest(canonical(before)), desired(store))
    assert store.read() == before


def test_two_competing_cas_operations_cannot_both_succeed(selector):
    store, path = selector
    before = digest(canonical(store.read()))
    def select(version):
        other = IsolatedSelection(path, h("lab-anchor"))
        try:
            other.compare_and_select(before, desired(other, version=version))
            return True
        except InstallError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(select, ("a", "b"))) == 1


def test_selector_history_tampering_is_not_a_new_trust_root(selector):
    store, _path = selector
    key = digest(canonical(store.read()))
    with store.db.connect(write=True) as connection:
        connection.execute("UPDATE history SET value=? WHERE sha256=?", (json.dumps(desired(store)), key))
    with pytest.raises(InstallError, match="changed"):
        store.historical(key)


def test_foreign_database_is_preserved_without_schema_repair(tmp_path):
    path = private_directory(tmp_path/"private")/"runtime-selection.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE business(value TEXT)")
    connection.commit()
    connection.close()
    tighten(path)
    before = path.read_bytes()
    with pytest.raises(InstallError, match="not an isolated selector"):
        IsolatedSelection(path, h("lab-anchor"))
    assert path.read_bytes() == before


def test_recovery_fence_can_only_transfer_from_the_expected_owner(selector):
    store, _path = selector
    store.fence("a"*32)
    with pytest.raises(InstallError):
        store.transfer_fence("b"*32, "c"*32)
    assert store.blocked() == "a"*32
    store.transfer_fence("a"*32, "c"*32)
    with pytest.raises(InstallError):
        store.clear_fence("a"*32)
    store.clear_fence("c"*32)
    assert store.blocked() is None
