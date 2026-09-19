"""Immutable, task-owned JSON artifacts and frozen principle navigation."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text, private_directory, private_file
from chatcopilot.harness.agent_types import ArtifactRef
from chatcopilot.harness.models import HarnessError


class ArtifactRepository:
    def __init__(self, directory: Path):
        self.directory = private_directory(directory)
        self.root = private_directory(self.directory / "artifacts")

    def put(self, kind: str, revision: int, value: Any) -> ArtifactRef:
        content = json_text(value).encode()
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / (digest + ".json")
        if path.exists():
            private_file(path)
            if path.read_bytes() != content:
                raise HarnessError("artifact_changed", "产物摘要与正文不符")
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
        return ArtifactRef(path.relative_to(self.directory).as_posix(), digest, kind, revision)

    def read(self, ref: ArtifactRef | dict[str, Any]) -> Any:
        ref = ArtifactRef(**ref) if isinstance(ref, dict) else ref
        if not re.fullmatch(r"[0-9a-f]{64}", ref.sha256) or ref.path != f"artifacts/{ref.sha256}.json":
            raise HarnessError("artifact_changed", "产物引用无效")
        path = self.directory / ref.path
        private_file(path)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != ref.sha256:
            raise HarnessError("artifact_changed", "产物正文已变化")
        return json.loads(content)

    def navigation(self, ref: ArtifactRef | dict[str, Any]) -> dict[str, Any]:
        ref = ArtifactRef(**ref) if isinstance(ref, dict) else ref
        value = self.read(ref)
        navigation = {**asdict(ref), "path": str(self.directory / ref.path)}
        if isinstance(value, dict):
            navigation["sections"] = {key: {"pointer": "/" + key.replace("~", "~0").replace("/", "~1"),
                "bytes": len(json_text(item).encode())} for key, item in value.items()}
            inline, omitted, used = {}, [], 0
            for key, item in value.items():
                size = len(json_text(item).encode())
                if size <= 4096 and used + size <= 12288:
                    inline[key] = item
                    used += size
                else:
                    omitted.append(key)
            navigation["inline_sections"] = inline
            navigation["omitted_sections"] = omitted
            navigation["inline_bytes"] = used
            if ref.kind == "principles":
                navigation["documents"] = [{"path": row["path"], "sha256": row["sha256"],
                    "pointer": f"/documents/{index}/content", "bytes": len(row["content"].encode())}
                    for index, row in enumerate(value["documents"])]
            if ref.kind == "governance_context":
                navigation["documents"] = [{"path": row["path"], "sha256": row["sha256"],
                    "pointer": f"/rules/{index}/content", "bytes": len(row["content"].encode())}
                    for index, row in enumerate(value["rules"])]
        special = {"source_index": 16384, "failure_brief": 8192, "target_context": 16384}.get(ref.kind, 4096)
        if len(json_text(value).encode()) <= special:
            navigation["inline"] = value
        return navigation

    def principles(self, source: Path) -> ArtifactRef:
        index = source / "docs/reference/harness-principles.md"
        if not index.is_file():
            raise HarnessError("principles_missing", "冻结源码缺少 Harness 黄金原则索引")
        text = index.read_text()
        paths = [index]
        for relative in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text):
            path = (index.parent / relative).resolve()
            if not path.is_relative_to(source.resolve()) or not path.is_file():
                raise HarnessError("principles_missing", "黄金原则引用不可用或超出源码范围")
            paths.append(path)
        documents = [{"path": path.relative_to(source).as_posix(),
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "content": path.read_text()}
                     for path in dict.fromkeys(paths)]
        return self.put("principles", 1, {"index": documents[0]["path"], "documents": documents})
