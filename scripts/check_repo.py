#!/usr/bin/env python3
"""Run deterministic local repository validation profiles."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_FAILED_NODE_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    cwd: Path = ROOT


def _python(*args: str) -> tuple[str, ...]:
    return (sys.executable, *args)


def _executable(name: str) -> str:
    return shutil.which(name) or name


def _pytest_basetemp(profile: str) -> str:
    root = Path(os.environ.get("TMPDIR") or "/tmp").expanduser().resolve()
    return f"--basetemp={root / f'chatcopilot-pytest-{profile}'}"


def _check_env() -> dict[str, str]:
    env = os.environ.copy()
    temp_root = str(Path(env.get("TMPDIR") or "/tmp").expanduser().resolve())
    env.update({"TMPDIR": temp_root, "TEMP": temp_root, "TMP": temp_root})
    return env


def _profiles() -> dict[str, tuple[Check, ...]]:
    common = (
        Check("SDD metadata", _python("scripts/check_sdd_specs.py")),
        Check("public repository boundary", _python("scripts/check_public_repo.py")),
        Check("architecture boundaries", _python("scripts/check_architecture.py")),
        Check("requirements drift", _python("scripts/sync_requirements.py", "--check")),
        Check("UTF-8 source normalization", _python("scripts/normalize_utf8.py")),
        Check("Ruff", _python("-m", "ruff", "check", "src", "tests", "scripts", "console")),
        Check(
            "typed contracts",
            _python(
                "-m",
                "mypy",
                "src/chatcopilot/contracts",
                "src/chatcopilot/agent/session_protocol.py",
            ),
        ),
        Check("component catalog", _python("scripts/check_component_catalog.py")),
    )
    fast = (
        *common,
        Check(
            "core tests",
            _python(
                "-m",
                "pytest",
                "tests/unit",
                "tests/integration/test_acp_streaming_updates.py",
                "tests/integration/test_lingye_botspec_smoke.py",
                "-q",
                _pytest_basetemp("fast"),
            ),
        ),
    )
    full = (
        *common,
        Check("installed dependency consistency", _python("-m", "pip", "check")),
        Check("Python wheel build smoke", _python("scripts/build_smoke.py")),
        Check(
            "full Python tests",
            _python(
                "-m",
                "pytest",
                "-q",
                _pytest_basetemp("full"),
            ),
        ),
        Check(
            "console production build",
            (_executable("npm"), "run", "build"),
            ROOT / "console" / "web",
        ),
    )
    return {"fast": fast, "full": full}


def _preflight(check: Check) -> str | None:
    executable = check.argv[0]
    if Path(executable).is_file() or shutil.which(executable):
        return None
    return f"required executable is unavailable: {executable}"


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "check"


def _failure_excerpt(output: str, *, limit: int = 12_000) -> str:
    text = str(output or "").strip()
    if not text:
        return ""
    marker = " FAILURES "
    marker_at = text.find(marker)
    if marker_at >= 0:
        line_at = text.rfind("\n", 0, marker_at)
        start = 0 if line_at < 0 else line_at + 1
        summary_at = text.find("short test summary info", marker_at)
        end = len(text) if summary_at < 0 else summary_at
        text = text[start:end].strip()
    return text[:limit]


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _write_manifest(report_dir: Path, payload: dict[str, object]) -> None:
    target = report_dir / "manifest.json"
    temp = report_dir / "manifest.json.tmp"
    _write_private(
        temp,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    temp.replace(target)
    try:
        target.chmod(0o600)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=("fast", "full"))
    parser.add_argument(
        "--report-dir",
        type=Path,
        help="write a private JSON manifest and complete per-check logs",
    )
    args = parser.parse_args()
    report_dir = args.report_dir.expanduser().resolve() if args.report_dir else None
    manifest: dict[str, object] = {
        "schema_version": 1,
        "profile": args.profile,
        "started_at": time.time(),
        "finished_at": None,
        "ok": False,
        "checks": [],
    }
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        try:
            report_dir.chmod(0o700)
        except OSError:
            pass
        _write_manifest(report_dir, manifest)

    for index, check in enumerate(_profiles()[args.profile], start=1):
        print(f"\n==> {check.name}", flush=True)
        started_at = time.time()
        record: dict[str, object] = {
            "name": check.name,
            "argv": list(check.argv),
            "cwd": str(check.cwd),
            "started_at": started_at,
            "finished_at": None,
            "elapsed_seconds": None,
            "status": "running",
            "exit_code": None,
            "failed_ids": [],
            "first_failure": "",
            "log_path": "",
        }
        checks = manifest["checks"]
        assert isinstance(checks, list)
        checks.append(record)
        error = _preflight(check)
        if error:
            print(error, file=sys.stderr)
            finished_at = time.time()
            record.update(
                {
                    "finished_at": finished_at,
                    "elapsed_seconds": round(finished_at - started_at, 3),
                    "status": "infra_error",
                    "exit_code": 2,
                    "first_failure": error,
                }
            )
            manifest.update({"finished_at": finished_at, "ok": False})
            if report_dir is not None:
                log_path = report_dir / f"{index:02d}-{_slug(check.name)}.log"
                _write_private(log_path, error + "\n")
                record["log_path"] = str(log_path)
                _write_manifest(report_dir, manifest)
            return 2
        if report_dir is None:
            completed = subprocess.run(
                check.argv,
                cwd=check.cwd,
                check=False,
                env=_check_env(),
            )
            combined_output = ""
        else:
            completed = subprocess.run(
                check.argv,
                cwd=check.cwd,
                check=False,
                env=_check_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            if stdout:
                print(stdout, end="" if stdout.endswith("\n") else "\n")
            if stderr:
                print(
                    stderr,
                    end="" if stderr.endswith("\n") else "\n",
                    file=sys.stderr,
                )
            combined_output = "\n".join(
                part
                for part in (
                    "--- stdout ---\n" + stdout if stdout else "",
                    "--- stderr ---\n" + stderr if stderr else "",
                )
                if part
            )
            log_path = report_dir / f"{index:02d}-{_slug(check.name)}.log"
            _write_private(log_path, combined_output)
            record["log_path"] = str(log_path)
        finished_at = time.time()
        failed_ids = (
            tuple(dict.fromkeys(_FAILED_NODE_RE.findall(combined_output)))
            if completed.returncode
            else ()
        )
        record.update(
            {
                "finished_at": finished_at,
                "elapsed_seconds": round(finished_at - started_at, 3),
                "status": "passed" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode,
                "failed_ids": list(failed_ids),
                "first_failure": (
                    _failure_excerpt(combined_output) if completed.returncode else ""
                ),
            }
        )
        if report_dir is not None:
            _write_manifest(report_dir, manifest)
        if completed.returncode:
            print(f"FAILED: {check.name} (exit {completed.returncode})", file=sys.stderr)
            manifest.update({"finished_at": finished_at, "ok": False})
            if report_dir is not None:
                _write_manifest(report_dir, manifest)
            return completed.returncode
    finished_at = time.time()
    manifest.update({"finished_at": finished_at, "ok": True})
    if report_dir is not None:
        _write_manifest(report_dir, manifest)
    print(f"\nOK: repository {args.profile} profile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
