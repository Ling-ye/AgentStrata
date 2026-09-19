#!/usr/bin/env python3
"""Run deterministic local repository validation profiles."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
_FAILED_NODE_RE = re.compile(r"^(?:FAILED|SUBFAILED\(.*?\))\s+(\S+)", re.MULTILINE)


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    cwd: Path = ROOT
    uses_repository_index: bool = False


def _python(*args: str) -> tuple[str, ...]:
    return (sys.executable, *args)


def _mypy(root: Path | None, *paths: str) -> tuple[str, ...]:
    if root is None:
        return _python("-m", "mypy", *paths)
    # Load the installed driver before exposing source paths as package metadata.
    # Mypy uses sys.path to distinguish local targets from untyped imports.
    driver = "\n".join((
        "import sys",
        "from mypy.main import main",
        "sys.path.extend(sys.argv[1:3])",
        "main(args=sys.argv[3:])",
    ))
    return _python("-I", "-c", driver, str(root), str(root / "src"), *paths)


def _executable(name: str) -> str:
    return shutil.which(name) or name


def _pytest_basetemp(profile: str) -> str:
    root = Path(os.environ.get("TMPDIR") or "/tmp").expanduser().resolve()
    return f"--basetemp={root / f'chatcopilot-pytest-{profile}'}"


def _check_env(*, uses_repository_index: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    temp_root = str(Path(env.get("TMPDIR") or "/tmp").expanduser().resolve())
    env.update({"TMPDIR": temp_root, "TEMP": temp_root, "TMP": temp_root})
    if uses_repository_index and env.get("GIT_INDEX_FILE"):
        env["GIT_OPTIONAL_LOCKS"] = "0"
    else:
        env.pop("GIT_INDEX_FILE", None)
        env.pop("GIT_OPTIONAL_LOCKS", None)
        env.pop("GIT_OBJECT_DIRECTORY", None)
        env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
    return env


def _fast_test_paths() -> tuple[str, ...]:
    paths = tuple(
        line.strip()
        for line in (ROOT / "tests" / "fast.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("tests/fast.txt must contain a nonempty, unique list of test files")
    for name in paths:
        path = Path(name)
        if (
            path.parts[0] != "tests"
            or ".." in path.parts
            or path.suffix != ".py"
            or not path.name.startswith("test_")
            or not (ROOT / path).is_file()
        ):
            raise ValueError(f"invalid fast test file: {name}")
    return paths


def _documentation_changes(root: Path, *, base_ref: str | None = None,
                           explicit: list[str] | None = None, isolated: bool = False) -> tuple[str, ...] | None:
    """Read changes in this checkout only; snapshots require caller-owned input."""
    names = set(explicit or ())
    for name in names:
        if not name or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or "\\" in name:
            raise ValueError("documentation changes require repository-relative paths")
    if isolated or not (root / ".git").exists():
        if base_ref is not None:
            raise ValueError("--docs-base requires a real checkout, not an isolated candidate or source snapshot")
        return tuple(sorted(names)) if explicit is not None else None

    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"

    def git(*args: str) -> bytes:
        try:
            result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
                                    env=environment, capture_output=True, check=False)
        except OSError as exc:
            raise ValueError("cannot collect documentation changes: Git could not be started") from exc
        if result.returncode:
            raise ValueError("cannot collect documentation changes: Git query or base reference failed")
        return result.stdout

    if Path(os.fsdecode(git("rev-parse", "--show-toplevel")).rstrip("\n")).resolve() != root.resolve():
        raise ValueError("documentation changes must come from the exact checked repository root")
    if base_ref is not None:
        if base_ref and len(base_ref) in {40, 64} and set(base_ref) == {"0"}:
            names.update(os.fsdecode(n) for n in git("ls-files", "-z").split(b"\0") if n)
        else:
            resolved = git("rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}").decode().strip()
            fields = iter(git("diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z",
                              "--find-renames", resolved, "--").split(b"\0"))
            for status in fields:
                if status:
                    names.add(os.fsdecode(next(fields)))
                    if status[:1] in {b"R", b"C"}:
                        names.add(os.fsdecode(next(fields)))
    # Porcelain -z handles unborn HEAD, staged and unstaged changes, and preserves
    # both rename paths without parsing Git's quoted display format.
    fields = iter(git("status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0"))
    for row in fields:
        if row:
            names.add(os.fsdecode(row[3:]))
            if b"R" in row[:2] or b"C" in row[:2]:
                names.add(os.fsdecode(next(fields)))
    return tuple(sorted(names))


def _profiles(candidate_root: Path | None = None) -> dict[str, tuple[Check, ...]]:
    test_root = candidate_root or ROOT
    test_imports = ("-o", "pythonpath=" + str(test_root / "src") + " " + str(test_root)) if candidate_root else ()
    target_args = ("--root", str(test_root)) if candidate_root else ()
    common = (
        Check("SDD metadata", _python("scripts/check_sdd_specs.py")),
        Check(
            "public repository boundary",
            _python("scripts/check_public_repo.py", *target_args),
            uses_repository_index=True,
        ),
        Check("documentation", _python("scripts/check_docs.py", *target_args)),
        Check("architecture boundaries", _python("scripts/check_architecture.py", *target_args)),
        Check("requirements drift", _python("scripts/sync_requirements.py", "--check")),
        Check(
            "UTF-8 source normalization",
            _python("scripts/normalize_utf8.py", *target_args),
            uses_repository_index=True,
        ),
        Check("Ruff", _python("-m", "ruff", "check",
                              *(str(candidate_root / name) for name in ("src", "tests", "scripts", "console")),
                              "--config", str(test_root / "pyproject.toml")) if candidate_root else
              _python("-m", "ruff", "check", "src", "tests", "scripts", "console"), cwd=test_root),
        Check(
            "typed contracts",
            _mypy(
                candidate_root,
                "src/chatcopilot/contracts",
                "src/chatcopilot/agent/session_protocol.py",
                "src/chatcopilot/evals/models.py",
                "src/chatcopilot/evals/result_codec.py",
                "src/chatcopilot/evals/trial_runner.py",
            ),
            cwd=test_root,
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
                *test_imports,
                *_fast_test_paths(),
                "-q",
                _pytest_basetemp("fast"),
            ),
            cwd=test_root,
        ),
    )
    full = (
        *common,
        Check("installed dependency consistency", _python("-m", "pip", "check")),
        Check(
            "Python wheel build smoke",
            _python("scripts/build_smoke.py", *(("--source-root", str(test_root)) if candidate_root else ())),
            uses_repository_index=True,
        ),
        Check(
            "full Python tests",
            _python(
                "-m",
                "pytest",
                *test_imports,
                "-q",
                _pytest_basetemp("full"),
            ),
            cwd=test_root,
        ),
        Check(
            "console production build",
            (_executable("npm"), "run", "build"),
            ROOT / "console" / "web",
        ),
    )
    docs = (
        common[0], common[1], common[2],
        Check("diff format", ("git", "diff", "--check"), cwd=test_root, uses_repository_index=True),
    )
    return {"docs": docs, "fast": fast, "full": full}


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


def _test_inventory(path: Path) -> dict[str, object]:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    identities = sorted({case.get("classname", "") + "::" + case.get("name", "") for case in cases})
    if not identities:
        raise ValueError("pytest did not report any executed test identities")
    skipped = sorted({case.get("classname", "") + "::" + case.get("name", "")
                      for case in cases if case.find("skipped") is not None})
    errors = sorted({case.get("classname", "") + "::" + case.get("name", "")
                     for case in cases if case.find("error") is not None})
    return {"sha256": hashlib.sha256(json.dumps(identities).encode()).hexdigest(),
            "count": len(identities), "skipped_ids": skipped, "error_ids": errors}


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
    parser.add_argument("profile", choices=("docs", "fast", "full"))
    parser.add_argument("--keep-going", action="store_true", help="collect every check for baseline comparison")
    parser.add_argument("--candidate-root", type=Path, help="isolated candidate test tree; checker definitions stay here")
    parser.add_argument("--docs-base", help="base commit/ref for documentation impact; defaults to local uncommitted changes")
    parser.add_argument("--changed-path", action="append", help="additional caller-owned changed path; repeat as needed")
    parser.add_argument(
        "--report-dir",
        type=Path,
        help="write a private JSON manifest and complete per-check logs",
    )
    args = parser.parse_args()
    try:
        documentation_changes = _documentation_changes(ROOT, base_ref=args.docs_base,
                                                      explicit=args.changed_path, isolated=args.candidate_root is not None)
    except ValueError as exc:
        parser.error(str(exc))
    report_dir = args.report_dir.expanduser().resolve() if args.report_dir else None
    manifest: dict[str, object] = {
        "schema_version": 1,
        "profile": args.profile,
        "started_at": time.time(),
        "finished_at": None,
        "ok": False,
        "checks": [],
        "documentation_changes": {"known": documentation_changes is not None,
                                  "base": args.docs_base, "paths": list(documentation_changes or ())},
    }
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        try:
            report_dir.chmod(0o700)
        except OSError:
            pass
        _write_manifest(report_dir, manifest)

    failure_exit = 0
    profiles = _profiles(args.candidate_root.resolve()) if args.candidate_root else _profiles()
    for index, check in enumerate(profiles[args.profile], start=1):
        if check.name == "documentation" and documentation_changes is not None:
            check = replace(check, argv=(*check.argv, "--changes-known",
                                        *(f"--changed-path={name}" for name in documentation_changes)))
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
                env=_check_env(
                    uses_repository_index=check.uses_repository_index
                ),
            )
            combined_output = ""
        else:
            junit = report_dir / f"{index:02d}-pytest.xml" if check.name in {"core tests", "full Python tests"} else None
            completed = subprocess.run(
                (*check.argv, *(("--junitxml=" + str(junit),) if junit else ())),
                cwd=check.cwd,
                check=False,
                env=_check_env(
                    uses_repository_index=check.uses_repository_index
                ),
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
        if report_dir is not None and check.name in {"core tests", "full Python tests"}:
            try:
                record["test_inventory"] = _test_inventory(junit)
            except (OSError, ValueError, ET.ParseError) as exc:
                completed.returncode = completed.returncode or 1
                combined_output += "\nIncomplete test evidence: " + str(exc)
                _write_private(log_path, combined_output)
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
            failure_exit = failure_exit or completed.returncode
            if args.keep_going:
                continue
            manifest.update({"finished_at": finished_at, "ok": False})
            if report_dir is not None:
                _write_manifest(report_dir, manifest)
            return completed.returncode
    finished_at = time.time()
    manifest.update({"finished_at": finished_at, "ok": not failure_exit})
    if report_dir is not None:
        _write_manifest(report_dir, manifest)
    print(f"\n{'FAILED' if failure_exit else 'OK'}: repository {args.profile} profile")
    return failure_exit


if __name__ == "__main__":
    raise SystemExit(main())
