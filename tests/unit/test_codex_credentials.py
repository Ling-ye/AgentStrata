from __future__ import annotations

import json
import multiprocessing
import stat
import threading
from pathlib import Path

import pytest

from chatcopilot.external_tools.codex_cli import credentials
from chatcopilot.external_tools.codex_cli.credentials import (
    CredentialBusyError,
    CredentialError,
    authoritative_auth_path,
    credential_lease,
    credential_lock,
    credential_status,
    install_login_credential,
)


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _write_auth(home: Path, token: str = "initial") -> Path:
    _private_dir(home)
    auth = home / "auth.json"
    auth.write_text(json.dumps(_auth_payload(token)), encoding="utf-8")
    auth.chmod(0o600)
    return auth


def _auth_payload(token: str) -> dict[str, object]:
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": f"id-{token}",
            "access_token": f"access-{token}",
            "refresh_token": token,
            "account_id": "test-account",
        },
        "last_refresh": "2026-07-28T00:00:00Z",
    }


def _refresh_token(path: Path) -> str:
    return str(json.loads(path.read_text())["tokens"]["refresh_token"])


def _install(root: Path, lane: str, token: str = "initial") -> int:
    staging = root.parent / f"staging-{lane}-{token}"
    _write_auth(staging, token)
    return install_login_credential(root, lane, staging)  # type: ignore[arg-type]


def _hold_lease(
    root_text: str,
    runtime_text: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    with credential_lease(Path(root_text), "main", Path(runtime_text)):
        entered.set()
        release.wait(timeout=10)


def test_status_distinguishes_missing_recognized_and_ready(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    assert credential_status(root, "main").to_dict() == {
        "lane": "main",
        "state": "missing",
        "credential_updated_at": None,
        "installed_at": None,
        "refreshed_at": None,
        "error_code": "auth_missing",
    }
    assert not root.exists()

    _write_auth(root)
    recognized = credential_status(root, "main")
    assert recognized.state == "recognized"
    assert recognized.generation == 0
    assert recognized.credential_updated_at is not None
    assert (root / ".locks").stat().st_mode & 0o777 == 0o700
    assert (root / ".locks" / "main.lock").stat().st_mode & 0o777 == 0o600

    staging = tmp_path / "staging"
    _write_auth(staging, "fresh")
    assert install_login_credential(
        root,
        "main",
        staging,
        installed_at="2026-07-28T12:00:00+00:00",
    ) == 1
    ready = credential_status(root, "main")
    assert ready.state == "ready"
    assert ready.generation == 1
    assert ready.installed_at == "2026-07-28T12:00:00+00:00"
    assert ready.error_code is None
    assert set(ready.to_dict()) == {
        "lane",
        "state",
        "credential_updated_at",
        "installed_at",
        "refreshed_at",
        "error_code",
    }


def test_explicit_logins_increment_only_the_selected_lane(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    assert _install(root, "main", "main-1") == 1
    assert _install(root, "worker", "worker-1") == 1
    assert _install(root, "main", "main-2") == 2

    assert _refresh_token(authoritative_auth_path(root, "main")) == "main-2"
    assert _refresh_token(authoritative_auth_path(root, "worker")) == "worker-1"
    assert credential_status(root, "main").generation == 2
    assert credential_status(root, "worker").generation == 1
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "worker").stat().st_mode & 0o777 == 0o700
    assert authoritative_auth_path(root, "main").stat().st_mode & 0o777 == 0o600
    metadata = json.loads((root / "credential.json").read_text())
    assert set(metadata) == {
        "schema_version",
        "generation",
        "installed_at",
        "refreshed_at",
        "last_error_code",
    }


def test_lease_copies_and_persists_refresh_without_incrementing_generation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    assert _install(root, "main") == 1
    runtime = tmp_path / "runtime"

    with credential_lease(root, "main", runtime) as lease:
        assert lease.generation == 1
        assert _refresh_token(runtime / "auth.json") == "initial"
        (runtime / "auth.json").write_text(
            json.dumps(_auth_payload("rotated")),
            encoding="utf-8",
        )
        (runtime / "auth.json").chmod(0o600)

    assert _refresh_token(authoritative_auth_path(root, "main")) == "rotated"
    status = credential_status(root, "main")
    assert status.generation == 1
    assert status.refreshed_at is not None


@pytest.mark.parametrize("exception_type", [RuntimeError, KeyboardInterrupt])
def test_lease_persists_valid_refresh_on_exceptional_exit(
    tmp_path: Path,
    exception_type: type[BaseException],
) -> None:
    root = tmp_path / "authority"
    _install(root, "main")
    runtime = tmp_path / "runtime"

    with pytest.raises(exception_type):
        with credential_lease(root, "main", runtime):
            (runtime / "auth.json").write_text(
                json.dumps(_auth_payload("rotated")),
                encoding="utf-8",
            )
            (runtime / "auth.json").chmod(0o600)
            raise exception_type()

    assert _refresh_token(authoritative_auth_path(root, "main")) == "rotated"


@pytest.mark.parametrize("bad_value", ["not json", "{}", "[]"])
def test_invalid_runtime_refresh_does_not_replace_authority(
    tmp_path: Path,
    bad_value: str,
) -> None:
    root = tmp_path / "authority"
    _install(root, "main")
    runtime = tmp_path / "runtime"

    with pytest.raises(CredentialError):
        with credential_lease(root, "main", runtime):
            (runtime / "auth.json").write_text(bad_value, encoding="utf-8")
            (runtime / "auth.json").chmod(0o600)

    assert _refresh_token(authoritative_auth_path(root, "main")) == "initial"


def test_overly_permissive_runtime_refresh_does_not_replace_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    _install(root, "main")
    runtime = tmp_path / "runtime"

    with pytest.raises(CredentialError, match="runtime_auth_permissions"):
        with credential_lease(root, "main", runtime):
            (runtime / "auth.json").write_text(
                json.dumps(_auth_payload("rotated")),
                encoding="utf-8",
            )
            (runtime / "auth.json").chmod(0o644)

    assert _refresh_token(authoritative_auth_path(root, "main")) == "initial"
    assert credential_status(root, "main").error_code == "runtime_auth_permissions"


def test_invalid_runtime_does_not_mask_original_exception(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    _install(root, "main")
    runtime = tmp_path / "runtime"

    with pytest.raises(RuntimeError, match="original"):
        with credential_lease(root, "main", runtime):
            (runtime / "auth.json").write_text("broken", encoding="utf-8")
            raise RuntimeError("original")

    assert _refresh_token(authoritative_auth_path(root, "main")) == "initial"
    assert credential_status(root, "main").error_code == "runtime_auth_invalid_json"


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (lambda auth: auth.write_text("broken", encoding="utf-8"), "auth_invalid_json"),
        (lambda auth: auth.chmod(0o644), "auth_permissions"),
    ],
)
def test_status_rejects_invalid_authoritative_credentials(
    tmp_path: Path,
    mutator: object,
    expected_code: str,
) -> None:
    root = tmp_path / "authority"
    auth = _write_auth(root)
    mutator(auth)  # type: ignore[operator]
    status = credential_status(root, "main")
    assert status.state == "invalid"
    assert status.error_code == expected_code
    with pytest.raises(CredentialError, match=expected_code):
        with credential_lease(root, "main", tmp_path / "runtime"):
            pass


def test_status_maps_post_read_stat_race_to_safe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    auth = _write_auth(root)
    real_stat = Path.stat

    def fail_auth_stat(path: Path, *args: object, **kwargs: object) -> object:
        if path == auth:
            raise OSError("private filesystem detail")
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", fail_auth_stat)

    status = credential_status(root, "main")

    assert status.state == "invalid"
    assert status.error_code == "auth_stat_failed"


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (
            {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "forbidden",
            },
            "auth_unsupported_mode",
        ),
        (
            {
                **_auth_payload("token"),
                "OPENAI_API_KEY": "forbidden",
            },
            "auth_api_key_forbidden",
        ),
        (
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {"access_token": "access-only"},
            },
            "auth_unrecognized",
        ),
    ],
)
def test_status_rejects_unsupported_auth_shapes(
    tmp_path: Path,
    payload: dict[str, object],
    expected_code: str,
) -> None:
    root = _private_dir(tmp_path / "authority")
    auth = root / "auth.json"
    auth.write_text(json.dumps(payload), encoding="utf-8")
    auth.chmod(0o600)

    status = credential_status(root, "main")

    assert status.state == "invalid"
    assert status.error_code == expected_code


def test_status_and_lease_reject_authoritative_symlink(tmp_path: Path) -> None:
    root = _private_dir(tmp_path / "authority")
    target = _write_auth(tmp_path / "elsewhere")
    (root / "auth.json").symlink_to(target)

    status = credential_status(root, "main")
    assert status.state == "invalid"
    assert status.error_code == "auth_symlink"
    with pytest.raises(CredentialError, match="auth_symlink"):
        with credential_lease(root, "main", tmp_path / "runtime"):
            pass


@pytest.mark.parametrize("kind", ["symlink", "permissions"])
def test_worker_authority_directory_must_be_private(
    tmp_path: Path,
    kind: str,
) -> None:
    root = _private_dir(tmp_path / "authority")
    worker = root / "worker"
    if kind == "symlink":
        target = tmp_path / "outside-worker"
        _write_auth(target)
        worker.symlink_to(target, target_is_directory=True)
        expected_code = "authority_home_symlink"
    else:
        _write_auth(worker)
        worker.chmod(0o755)
        expected_code = "authority_home_permissions"

    status = credential_status(root, "worker")

    assert status.state == "invalid"
    assert status.error_code == expected_code
    with pytest.raises(CredentialError, match=expected_code):
        with credential_lease(root, "worker", tmp_path / "runtime"):
            pass


def test_overly_permissive_staging_is_not_installed(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    _install(root, "main", "old")
    staging = tmp_path / "staging"
    auth = _write_auth(staging, "new")
    auth.chmod(0o644)

    with pytest.raises(CredentialError, match="staging_auth_permissions"):
        install_login_credential(root, "main", staging)

    assert _refresh_token(authoritative_auth_path(root, "main")) == "old"
    assert credential_status(root, "main").generation == 1


def test_install_write_failure_rolls_back_generation_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    old_auth = _write_auth(root, "old")
    staging = tmp_path / "staging"
    _write_auth(staging, "new")
    real_atomic_write = credentials._atomic_write
    failed_once = False

    def fail_authority_auth(path: Path, payload: bytes) -> None:
        nonlocal failed_once
        if path == old_auth and not failed_once:
            failed_once = True
            raise CredentialError("credential_write_failed")
        real_atomic_write(path, payload)

    monkeypatch.setattr(credentials, "_atomic_write", fail_authority_auth)

    with pytest.raises(CredentialError, match="credential_write_failed"):
        install_login_credential(root, "main", staging)

    assert _refresh_token(old_auth) == "old"
    assert not (root / "credential.json").exists()
    assert credential_status(root, "main").state == "recognized"


def test_install_post_replace_failure_restores_auth_before_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    assert _install(root, "main", "old") == 1
    authority_auth = authoritative_auth_path(root, "main")
    old_auth = authority_auth.read_bytes()
    old_metadata = (root / "credential.json").read_bytes()
    staging = tmp_path / "staging-new"
    _write_auth(staging, "new")
    real_atomic_write = credentials._atomic_write
    failed_after_replace = False

    def replace_then_fail(path: Path, payload: bytes) -> None:
        nonlocal failed_after_replace
        if path == authority_auth and not failed_after_replace:
            failed_after_replace = True
            real_atomic_write(path, payload)
            raise CredentialError("credential_write_failed")
        real_atomic_write(path, payload)

    monkeypatch.setattr(credentials, "_atomic_write", replace_then_fail)

    with pytest.raises(CredentialError, match="credential_write_failed"):
        install_login_credential(root, "main", staging)

    assert authority_auth.read_bytes() == old_auth
    assert (root / "credential.json").read_bytes() == old_metadata
    assert credential_status(root, "main").generation == 1


def test_install_auth_rollback_failure_keeps_new_generation_fail_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    assert _install(root, "main", "old") == 1
    authority_auth = authoritative_auth_path(root, "main")
    staging = tmp_path / "staging-new"
    _write_auth(staging, "new")
    real_atomic_write = credentials._atomic_write
    authority_writes = 0

    def fail_replace_and_rollback(path: Path, payload: bytes) -> None:
        nonlocal authority_writes
        if path == authority_auth:
            authority_writes += 1
            if authority_writes == 1:
                real_atomic_write(path, payload)
                raise CredentialError("credential_write_failed")
            raise CredentialError("credential_write_failed")
        real_atomic_write(path, payload)

    monkeypatch.setattr(credentials, "_atomic_write", fail_replace_and_rollback)

    with pytest.raises(
        CredentialError,
        match="credential_install_rollback_failed",
    ):
        install_login_credential(root, "main", staging)

    assert _refresh_token(authority_auth) == "new"
    assert credential_status(root, "main").generation == 2


def test_runtime_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    _install(root, "main")
    runtime = _private_dir(tmp_path / "runtime")
    outside = _write_auth(tmp_path / "outside", "outside")
    (runtime / "auth.json").symlink_to(outside)

    with pytest.raises(CredentialError, match="runtime_auth_symlink"):
        with credential_lease(root, "main", runtime):
            pass

    assert _refresh_token(outside) == "outside"
    assert _refresh_token(authoritative_auth_path(root, "main")) == "initial"


def test_nonblocking_status_reports_same_lane_busy_but_other_lane_independent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    _install(root, "main")
    _install(root, "worker")

    with credential_lock(root, "main"):
        main = credential_status(root, "main")
        worker = credential_status(root, "worker")

    assert main.state == "busy"
    assert main.error_code == "lock_busy"
    assert worker.state == "ready"


def test_held_login_lock_prevents_competing_installer(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    staging = tmp_path / "staging"
    _write_auth(staging)

    with credential_lock(root, "main", blocking=False) as held:
        with pytest.raises(CredentialBusyError):
            install_login_credential(root, "main", staging)
        assert install_login_credential(root, "main", staging, held_lock=held) == 1


def test_cross_process_lease_contention_reports_busy(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    _install(root, "main")
    context = multiprocessing.get_context("fork")
    entered = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_lease,
        args=(str(root), str(tmp_path / "child-runtime"), entered, release),
    )
    process.start()
    try:
        assert entered.wait(timeout=5)
        assert credential_status(root, "main").state == "busy"
        with pytest.raises(CredentialBusyError):
            with credential_lease(
                root,
                "main",
                tmp_path / "parent-runtime",
                blocking=False,
            ):
                pass
    finally:
        release.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0


def test_main_and_worker_leases_can_overlap(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    _install(root, "main")
    _install(root, "worker")
    main_entered = threading.Event()
    worker_entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold(lane: str, entered: threading.Event) -> None:
        try:
            with credential_lease(
                root,
                lane,  # type: ignore[arg-type]
                tmp_path / f"{lane}-runtime",
            ):
                entered.set()
                release.wait(timeout=5)
        except BaseException as exc:
            errors.append(exc)

    main_thread = threading.Thread(target=hold, args=("main", main_entered))
    worker_thread = threading.Thread(target=hold, args=("worker", worker_entered))
    main_thread.start()
    worker_thread.start()
    try:
        assert main_entered.wait(timeout=5)
        assert worker_entered.wait(timeout=5)
    finally:
        release.set()
        main_thread.join(timeout=5)
        worker_thread.join(timeout=5)
    assert not errors


def test_first_cross_lane_lock_initialization_tolerates_directory_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _private_dir(tmp_path / "authority")
    lock_home = root / ".locks"
    barrier = threading.Barrier(2)
    real_mkdir = Path.mkdir
    errors: list[BaseException] = []

    def raced_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == lock_home:
            barrier.wait(timeout=5)
        real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]

    def acquire(lane: str) -> None:
        try:
            with credential_lock(root, lane):  # type: ignore[arg-type]
                pass
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(Path, "mkdir", raced_mkdir)
    main_thread = threading.Thread(target=acquire, args=("main",))
    worker_thread = threading.Thread(target=acquire, args=("worker",))
    main_thread.start()
    worker_thread.start()
    main_thread.join(timeout=5)
    worker_thread.join(timeout=5)

    assert not main_thread.is_alive()
    assert not worker_thread.is_alive()
    assert not errors
    assert stat.S_IMODE(lock_home.stat().st_mode) == 0o700


def test_no_personal_codex_home_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    personal = tmp_path / "personal"
    _write_auth(personal, "personal-secret")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(personal))
    root = tmp_path / "missing-authority"

    assert credential_status(root, "main").state == "missing"
    with pytest.raises(CredentialError, match="auth_root_missing"):
        with credential_lease(root, "main", tmp_path / "runtime"):
            pass
    assert not (tmp_path / "runtime" / "auth.json").exists()
