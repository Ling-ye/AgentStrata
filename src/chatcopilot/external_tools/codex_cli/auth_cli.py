"""Deterministic operator workflow for independent Codex device authentication."""
from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal, Mapping

from chatcopilot.external_tools.codex_cli.credentials import (
    CredentialBusyError,
    CredentialError,
    CredentialLane,
    CredentialStatus,
    credential_lock,
    credential_status,
    install_login_credential_data,
    load_staged_login_credential,
    validate_auth_root_path,
)

LaneSelection = Literal["main", "worker", "all"]

_LOGIN_TIMEOUT_SECONDS = 16 * 60
_PREFLIGHT_TIMEOUT_SECONDS = 15
_TERMINATE_GRACE_SECONDS = 5
_DEVICE_AUTH_SETTING = 'cli_auth_credentials_store="file"'
_PASSTHROUGH_ENV_KEYS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "LANG",
    "LC_ALL",
    "TERM",
    "TZ",
)


class CodexAuthOperatorError(RuntimeError):
    """A stable, non-secret error suitable for operator output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CodexAuthOperatorConfig:
    """Validated fixed paths needed by the operator workflow."""

    auth_root: Path
    codex_bin: Path

    @classmethod
    def from_values(cls, auth_root: str, codex_bin: str) -> "CodexAuthOperatorConfig":
        root = validate_auth_root(auth_root)
        binary = Path(codex_bin)
        if not codex_bin.strip():
            raise CodexAuthOperatorError("codex_binary_missing_config")
        if not binary.is_absolute():
            raise CodexAuthOperatorError("codex_binary_not_absolute")
        try:
            info = binary.stat()
        except OSError as exc:
            raise CodexAuthOperatorError("codex_binary_missing") from exc
        if not stat.S_ISREG(info.st_mode):
            raise CodexAuthOperatorError("codex_binary_not_regular")
        if not os.access(binary, os.X_OK):
            raise CodexAuthOperatorError("codex_binary_not_executable")
        return cls(auth_root=root, codex_bin=binary)


@dataclass(frozen=True)
class CodexAuthLoginResult:
    """One lane's safe login outcome."""

    lane: CredentialLane
    ok: bool
    generation: int | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "ok": self.ok,
            "generation": self.generation,
            "error_code": self.error_code,
        }


def selected_lanes(selection: LaneSelection) -> tuple[CredentialLane, ...]:
    """Expand the public lane selector into its deterministic execution order."""

    if selection == "main":
        return ("main",)
    if selection == "worker":
        return ("worker",)
    if selection == "all":
        return ("main", "worker")
    raise ValueError("lane must be main, worker, or all")


def validate_auth_root(auth_root: str) -> Path:
    """Validate that an authority is absolute and not personal Codex state."""

    try:
        return validate_auth_root_path(auth_root)
    except CredentialError as exc:
        raise CodexAuthOperatorError(exc.code) from exc


def login_lanes(
    config: CodexAuthOperatorConfig,
    selection: LaneSelection,
) -> tuple[CodexAuthLoginResult, ...]:
    """Run one or two independent device authorizations.

    Capability probing captures help output only in memory and never
    exposes it. The actual device flow inherits the operator terminal so the
    verification URL and one-time code remain interactive and are not logged by
    this wrapper.
    """

    lanes = selected_lanes(selection)
    try:
        preflight_error = _device_auth_preflight(config)
    except CodexAuthOperatorError as exc:
        preflight_error = exc.code
    if preflight_error is not None:
        return tuple(
            CodexAuthLoginResult(lane=lane, ok=False, error_code=preflight_error)
            for lane in lanes
        )

    results: list[CodexAuthLoginResult] = []
    for lane in lanes:
        result = _login_lane(config, lane)
        results.append(result)
        if not result.ok:
            break
    return tuple(results)


def status_lanes(
    auth_root: Path,
    selection: LaneSelection,
) -> tuple[CredentialStatus, ...]:
    """Read safe credential status for the selected lanes."""

    return tuple(credential_status(auth_root, lane) for lane in selected_lanes(selection))


def _device_auth_preflight(config: CodexAuthOperatorConfig) -> str | None:
    with _private_staging_home("preflight") as staging_home:
        env = _isolated_login_env(staging_home)
        try:
            process = subprocess.Popen(
                [str(config.codex_bin), "login", "--help"],
                cwd=str(staging_home),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            return "codex_launch_failed"
        try:
            stdout, stderr = process.communicate(timeout=_PREFLIGHT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            return "device_auth_preflight_timeout"
        except KeyboardInterrupt:
            _terminate_process_group(process)
            return "device_auth_cancelled"
        except OSError:
            _terminate_process_group(process)
            return "device_auth_preflight_failed"
        if process.returncode != 0:
            return "device_auth_preflight_failed"
        help_output = stdout + stderr
        if b"--device-auth" not in help_output:
            return "device_auth_unsupported"
    return None


def _login_lane(
    config: CodexAuthOperatorConfig,
    lane: CredentialLane,
) -> CodexAuthLoginResult:
    try:
        with credential_lock(
            config.auth_root,
            lane,
            blocking=False,
            create=True,
        ) as lock:
            staged_auth: bytes
            with _private_staging_home(lane) as staging_home:
                error_code = _run_device_login(config, staging_home)
                if error_code is not None:
                    return CodexAuthLoginResult(
                        lane=lane,
                        ok=False,
                        error_code=error_code,
                    )
                staged_auth = load_staged_login_credential(staging_home)
            generation = install_login_credential_data(
                config.auth_root,
                lane,
                staged_auth,
                held_lock=lock,
            )
            return CodexAuthLoginResult(
                lane=lane,
                ok=True,
                generation=generation,
            )
    except CredentialBusyError:
        return CodexAuthLoginResult(lane=lane, ok=False, error_code="lock_busy")
    except CredentialError as exc:
        return CodexAuthLoginResult(lane=lane, ok=False, error_code=exc.code)
    except CodexAuthOperatorError as exc:
        return CodexAuthLoginResult(lane=lane, ok=False, error_code=exc.code)


def _run_device_login(
    config: CodexAuthOperatorConfig,
    staging_home: Path,
) -> str | None:
    command = [
        str(config.codex_bin),
        "login",
        "--device-auth",
        "-c",
        _DEVICE_AUTH_SETTING,
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(staging_home),
            env=_isolated_login_env(staging_home),
            start_new_session=True,
        )
    except OSError:
        return "codex_launch_failed"
    try:
        return_code = process.wait(timeout=_LOGIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        return "device_auth_timeout"
    except KeyboardInterrupt:
        _terminate_process_group(process)
        return "device_auth_cancelled"
    except OSError:
        _terminate_process_group(process)
        return "device_auth_failed"
    if return_code != 0:
        return "device_auth_failed"
    return None


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        return
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        return
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return


@contextmanager
def _private_staging_home(label: str) -> Iterator[Path]:
    try:
        path = Path(tempfile.mkdtemp(prefix=f"chatcopilot-codex-auth-{label}-"))
    except OSError as exc:
        raise CodexAuthOperatorError("staging_create_failed") from exc
    try:
        path.chmod(0o700)
    except OSError as exc:
        shutil.rmtree(path, ignore_errors=True)
        raise CodexAuthOperatorError("staging_create_failed") from exc
    try:
        yield path
    finally:
        auth_path = path / "auth.json"
        try:
            auth_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if os.path.lexists(path):
                raise CodexAuthOperatorError("staging_cleanup_failed") from exc


def _isolated_login_env(staging_home: Path) -> Mapping[str, str]:
    env = {
        key: value
        for key in _PASSTHROUGH_ENV_KEYS
        if (value := os.environ.get(key))
    }
    home = str(staging_home)
    env.update(
        {
            "HOME": home,
            "CODEX_HOME": home,
            "CODEX_SQLITE_HOME": home,
            "XDG_CACHE_HOME": str(staging_home / ".cache"),
            "XDG_CONFIG_HOME": str(staging_home / ".config"),
            "XDG_DATA_HOME": str(staging_home / ".local" / "share"),
            "TMPDIR": str(staging_home),
            "TEMP": str(staging_home),
            "TMP": str(staging_home),
        }
    )
    return env


__all__ = [
    "CodexAuthLoginResult",
    "CodexAuthOperatorConfig",
    "CodexAuthOperatorError",
    "LaneSelection",
    "login_lanes",
    "selected_lanes",
    "status_lanes",
    "validate_auth_root",
]
