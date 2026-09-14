"""Export candidate bytes as a patch without staging or creating commits."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from chatcopilot.harness.models import HarnessError


def save_patch(root: Path, frozen: Path, names: list[str], output: Path) -> str:
    """Use Git's binary patch encoding without staging or creating a baseline commit."""
    chunks = []
    for name in names:
        before, after = frozen / name, root / name
        result = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--no-ext-diff", "--no-textconv", "--",
             str(before) if before.exists() else "/dev/null",
             str(after) if after.exists() else "/dev/null"],
            capture_output=True, timeout=30, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        if result.returncode not in {0, 1}:
            raise HarnessError("patch_failed", "无法导出工作区快照补丁")
        content = result.stdout
        for prefix in (str(frozen).lstrip("/"), str(root).lstrip("/")):
            content = content.replace(("a/" + prefix + "/").encode(), b"a/")
            content = content.replace(("b/" + prefix + "/").encode(), b"b/")
        chunks.append(content)
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    content = b"".join(chunks)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
    return hashlib.sha256(content).hexdigest()
