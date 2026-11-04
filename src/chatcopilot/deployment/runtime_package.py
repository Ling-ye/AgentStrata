"""Source-build packages for first installation, distinct from accepted candidates."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from chatcopilot.core.source_snapshot import copy_sources, git_output, manifest_digest, source_manifest

from chatcopilot.deployment.application_bundle import NATIVE_SCHEMA as SCHEMA


def export_runtime(source: Path, output: Path, *, bot_relative="bots/live2d-desktop/bot.yaml") -> dict:
    """Freeze the source stage; the producer must bind artifacts before read_bundle accepts it."""
    from chatcopilot.deployment.application_bundle import MANIFEST
    if output.exists():
        raise ValueError("Runtime package output already exists")
    manifest = source_manifest(source)
    if bot_relative not in manifest:
        raise ValueError("Runtime package application is missing")
    from chatcopilot.core.application_configuration import load_application_spec
    spec = load_application_spec(source / bot_relative)
    record = {"schema": SCHEMA, "bot_id": spec.raw["id"], "bot_relative": bot_relative,
        "source_manifest": manifest, "source_digest": manifest_digest(manifest),
        "origin": {"kind": "source-build", "base_revision": git_output(source, "rev-parse", "HEAD")},
        "platform": "windows-x86_64"}
    try:
        copy_sources(source, output / "source", manifest)
        (output / MANIFEST).write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    except BaseException:
        shutil.rmtree(output)
        raise
    return record

