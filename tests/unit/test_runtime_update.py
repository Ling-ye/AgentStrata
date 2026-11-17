"""Real private journal transactions with synthetic admissions; no image is switched."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.storage_files import tighten
from chatcopilot.deployment.runtime_acceptance import canonical, digest
from chatcopilot.deployment.runtime_update import RuntimeUpdateJournal
from tests.unit.test_runtime_acceptance import admit, documents, h


def observation(record, selected, **extra):
    return canonical({"schema":"agentstrata-runtime-switch-observation/v1", "identity_sha256":record["identity_sha256"],
        "intent_sha256":record["intent_sha256"], "selected_sha256":selected, "verification_sha256":h("synthetic selection observation"), **extra})


def reconcile(journal, record, selected, **extra):
    raw = observation(record, selected, **extra)
    return journal.reconcile(record["id"], raw, expected_observation_sha256=digest(raw))


@pytest.fixture
def journal(tmp_path):
    path = private_directory(tmp_path/"private")/"runtime-update.sqlite"
    a = admit(documents("a"))
    return RuntimeUpdateJournal(path, a.binding), a, path


def initial(journal, a):
    record = journal.prepare(a, current=None, expected_generation=0, operation_id="a"*32)
    assert reconcile(journal, record, a.receipt_sha256)["state"] == "recorded"
    return record


def test_restart_replay_and_generation_are_durable_without_touching_images_or_data(journal, tmp_path):
    store, a, path = journal
    data = tmp_path/"business-data"
    data.write_bytes(b"preserve mutable business data")
    first = initial(store, a)
    reopened = RuntimeUpdateJournal(path, a.binding)
    assert reopened.inspect() == store.inspect()
    assert reopened.inspect()["generation"] == 1
    assert reopened.prepare(a, current=None, expected_generation=0, operation_id="a"*32)["state"] == "recorded"
    assert reconcile(reopened, first, a.receipt_sha256)["state"] == "recorded"
    assert reopened.inspect()["generation"] == 1 and data.read_bytes() == b"preserve mutable business data"


def test_only_the_current_update_can_roll_back_to_its_admitted_predecessor(journal):
    store, a, _path = journal
    b = admit(documents("b"))
    initial(store, a)
    update = store.prepare(b, current=a, expected_generation=1, operation_id="b"*32)
    assert reconcile(store, update, b.receipt_sha256)["state"] == "recorded"
    rollback = store.prepare(a, current=b, expected_generation=2, operation_id="c"*32, rollback_of=update["id"])
    assert rollback["kind"] == "rollback" and store.operation(update["id"])["state"] == "recorded"
    assert reconcile(store, rollback, a.receipt_sha256)["state"] == "recorded"
    assert store.inspect()["generation"] == 3 and store.inspect()["selected_sha256"] == a.receipt_sha256
    with pytest.raises(InstallError):
        store.prepare(a, current=b, expected_generation=3, rollback_of=update["id"])


def test_ambiguous_observation_blocks_new_work_until_independent_reconciliation(journal):
    store, a, path = journal
    initial(store, a)
    b = admit(documents("b"))
    record = store.prepare(b, current=a, expected_generation=1, operation_id="b"*32)
    assert reconcile(store, record, h("unknown selected image"))["state"] == "recovery_required"
    assert store.inspect()["selected_sha256"] == a.receipt_sha256 and store.inspect()["pending"] == record["id"]
    with pytest.raises(InstallError, match="unresolved"):
        store.prepare(admit(documents("c")), current=a, expected_generation=1)
    reopened = RuntimeUpdateJournal(path, a.binding)
    assert reconcile(reopened, record, b.receipt_sha256)["state"] == "recorded"
    assert reopened.inspect()["pending"] is None


def test_restoration_preserves_old_selection_and_allows_a_new_fresh_intent(journal):
    store, a, _path = journal
    initial(store, a)
    b = admit(documents("b"))
    record = store.prepare(b, current=a, expected_generation=1)
    assert reconcile(store, record, a.receipt_sha256)["state"] == "restored"
    assert store.inspect()["generation"] == 1 and store.inspect()["pending"] is None
    fresh = store.prepare(b, current=a, expected_generation=1)
    assert fresh["id"] != record["id"]


@pytest.mark.parametrize("case", ["stale-generation", "wrong-current", "changed-retry", "wrong-rollback", "foreign-install", "boolean-generation"])
def test_stale_foreign_or_conflicting_intents_cannot_advance_selection(journal, case):
    store, a, _path = journal
    initial(store, a)
    b, c = admit(documents("b")), admit(documents("c"))
    options = {"current":a, "expected_generation":1}
    target = b
    if case == "stale-generation":
        options["expected_generation"] = 0
    elif case == "wrong-current":
        options["current"] = c
    elif case == "changed-retry":
        store.prepare(b, **options, operation_id="b"*32)
        target = c
        options["operation_id"] = "b"*32
    elif case == "wrong-rollback":
        update = store.prepare(b, **options)
        reconcile(store, update, b.receipt_sha256)
        options = {"current":b, "expected_generation":2, "rollback_of":update["id"]}
        target = c
    elif case == "foreign-install":
        target = replace(b, binding=replace(b.binding, data_root_sha256=h("other-data-root")))
    else:
        options["expected_generation"] = True
    before = store.inspect()
    with pytest.raises(InstallError):
        store.prepare(target, **options)
    assert store.inspect() == before


@pytest.mark.parametrize("change", ["pin", "identity", "intent", "boolean-selection", "extra", "verification"])
def test_selection_observation_requires_external_pin_and_exact_intent(journal, change):
    store, a, _path = journal
    record = store.prepare(a, current=None, expected_generation=0)
    value = json.loads(observation(record, a.receipt_sha256))
    if change == "identity":
        value["identity_sha256"] = h("other-installation")
    elif change == "intent":
        value["intent_sha256"] = h("other-operation")
    elif change == "boolean-selection":
        value["selected_sha256"] = True
    elif change == "extra":
        value["extra"] = "unknown"
    elif change == "verification":
        value["verification_sha256"] = False
    raw = canonical(value)
    expected = h("wrong pin") if change == "pin" else digest(raw)
    before = store.inspect()
    with pytest.raises(InstallError):
        store.reconcile(record["id"], raw, expected_observation_sha256=expected)
    assert store.inspect() == before


def test_concurrent_writers_cannot_prepare_two_updates_for_the_same_generation(journal):
    store, a, path = journal
    initial(store, a)
    def prepare(version):
        other = RuntimeUpdateJournal(path, a.binding)
        try:
            return other.prepare(admit(documents(version)), current=a, expected_generation=1)["id"]
        except InstallError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, ("b", "c")))
    assert sum(result is not None for result in results) == 1
    assert store.inspect()["generation"] == 1 and store.inspect()["pending"] in results


def test_journal_rejects_foreign_database_without_initializing_or_repairing_it(tmp_path):
    path = private_directory(tmp_path/"private")/"runtime-update.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE business (value TEXT)")
    connection.execute("INSERT INTO business VALUES ('preserve')")
    connection.commit()
    connection.close()
    tighten(path)
    before = path.read_bytes()
    with pytest.raises(InstallError, match="not a runtime update journal"):
        RuntimeUpdateJournal(path, documents()[2])
    assert path.read_bytes() == before


def test_reopening_for_other_identity_and_tampering_with_intent_are_rejected(journal):
    store, a, path = journal
    with pytest.raises(InstallError, match="another installation"):
        RuntimeUpdateJournal(path, replace(a.binding, owner_sid="S-1-5-21-1-2-3-1002"))
    record = store.prepare(a, current=None, expected_generation=0)
    record["target_sha256"] = h("substituted image")
    with store.db.connect(write=True) as connection:
        connection.execute("UPDATE operations SET record=? WHERE id=?", (canonical(record).decode(), record["id"]))
    with pytest.raises(InstallError, match="intent"):
        store.operation(record["id"])


def test_relative_or_general_database_paths_are_rejected_before_creation(tmp_path):
    for path in (Path("runtime-update.sqlite"), tmp_path/"business.sqlite"):
        with pytest.raises(InstallError, match="explicit private"):
            RuntimeUpdateJournal(path, documents()[2])
        assert not path.exists()
