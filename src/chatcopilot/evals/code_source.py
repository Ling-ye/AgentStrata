"""Generic candidate-source preparation; no dependency on repair orchestration."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.core.candidate_configuration import configuration_path, validate_configuration
from chatcopilot.core.source_snapshot import (
    copy_sources,
    git_output,
    manifest_digest,
    source_manifest,
    verify_copy,
)

# These paths define the test, not the product being tested.
_TRUSTED_PREFIXES = ("src/chatcopilot/evals/", "src/chatcopilot/harness/", "tests/")
_TRUSTED_FILES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "src/chatcopilot/__init__.py",
        *(
            "src/chatcopilot/core/" + name
            for name in (
                "file_integrity.py",
                "private_sqlite.py",
                "source_snapshot.py",
                "source_manifest.py",
                "inspection.py",
                "candidate_configuration.py",
            )
        ),
    }
)


def prepare_code_source(
    repository: Path, source: Mapping[str, Any], output: Path
) -> dict[str, Any]:
    if set(source) != {"path", "sha256"}:
        raise ValueError("code source requires path and sha256")
    path = Path(str(source["path"])).absolute()
    if path.resolve() != path or path.stat().st_uid != os.getuid():
        raise ValueError("candidate source must be a canonical user-owned worktree")
    origin = git_output(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if git_output(path, "rev-parse", "--path-format=absolute", "--git-common-dir") != origin:
        raise ValueError("candidate source must belong to the service repository")
    candidate = source_manifest(path)
    if manifest_digest(candidate) != source["sha256"]:
        raise ValueError("candidate source changed since submission")
    trusted = source_manifest(repository)
    for name in candidate.keys() | trusted.keys():
        if not name.startswith("bots/") or candidate.get(name) == trusted.get(name):
            continue
        if not configuration_path(name):
            raise ValueError("candidate changes a protected Bot resource")
        if name.endswith("/bot.yaml"):
            if name not in candidate or name not in trusted:
                raise ValueError("candidate cannot add or remove a Bot runtime envelope")
            validate_configuration((repository / name).read_bytes(), (path / name).read_bytes())
    members = {
        name: (repository if name.startswith(_TRUSTED_PREFIXES) or name in _TRUSTED_FILES else path)
        for name in candidate
    }
    for name in trusted:
        if name.startswith(_TRUSTED_PREFIXES) or name in _TRUSTED_FILES:
            members[name] = repository
    manifest = {
        name: (trusted if root == repository else candidate)[name]
        for name, root in members.items()
        if name in (trusted if root == repository else candidate)
    }
    try:
        for root in dict.fromkeys((path, repository)):
            subset = {name: value for name, value in manifest.items() if members[name] == root}
            if subset:
                copy_sources(root, output, subset)
        if manifest_digest(source_manifest(path)) != source["sha256"]:
            raise ValueError("candidate source changed during preparation")
        verify_copy(output, manifest)
    except BaseException:
        if output.is_dir() and not output.is_symlink():
            shutil.rmtree(output)
        raise
    return {
        "path": str(output),
        "sha256": source["sha256"],
        "execution_sha256": manifest_digest(manifest),
        "manifest": manifest,
        "commit": git_output(path, "rev-parse", "HEAD"),
        "trusted_evaluation_sha256": manifest_digest(
            {n: v for n, v in manifest.items() if n.startswith("src/chatcopilot/evals/")}
        ),
    }


def verify_code_source(source: Mapping[str, Any]) -> None:
    manifest = source.get("manifest")
    if not isinstance(manifest, dict) or manifest_digest(manifest) != source.get(
        "execution_sha256"
    ):
        raise ValueError("invalid frozen source manifest")
    verify_copy(Path(str(source["path"])), manifest)


def write_source_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json_text(dict(receipt)))


def read_source_receipt(path: Path) -> dict[str, Any]:
    from chatcopilot.core.private_sqlite import private_file

    private_file(path)
    with path.open() as stream:
        result = json.load(stream)
    verify_code_source(result)
    return result
