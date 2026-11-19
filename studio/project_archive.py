"""Package an accepted application as browser assets or a complete source project."""

import json
import tempfile
from pathlib import Path, PureWindowsPath
from zipfile import ZIP_DEFLATED, ZipFile

from chatcopilot.core.source_snapshot import copy_sources
from studio.delivery import digest


def pack(release, destination, bot, target):
    prefix = f"integrations/{bot}/" if target == "web" else ""
    entries = {
        name: data for name, data in release.candidate_manifest.items() if name.startswith(prefix)
    }
    if target == "web" and prefix + "index.html" not in entries:
        raise ValueError("已验收候选中缺少网页入口")
    for name in entries:
        member = name.removeprefix(prefix)
        if (
            "\\" in member
            or PureWindowsPath(member).drive
            or ".." in Path(member).parts
            or member.startswith("/")
        ):
            raise ValueError("候选文件不能作为跨平台归档成员")
    destination.mkdir(parents=True)
    archive = destination / "application.zip"
    with tempfile.TemporaryDirectory(dir=destination) as tmp:
        root = Path(tmp)
        copy_sources(release.candidate, root, entries)
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
            for name in sorted(entries):
                bundle.write(root / name, name.removeprefix(prefix))
    (destination / "build-receipt.json").write_text(
        json.dumps(
            {
                "accepted_source_digest": release.receipt["candidate_digest"],
                "archive_sha256": digest(archive),
            }
        )
    )
