"""Host observations of ordinary task files and successful native reads."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shlex

from chatcopilot.contracts.execution_scope import ExecutionScope, bind_execution_scope
from chatcopilot.contracts.workspace import IDENTITY_FILENAME
from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.scoped_files import read_bytes

# Same per-body bound as Evaluation's execution capture; oversize is explicit.
TEXT_BYTES = 128 * 1024


def file_record(root: Path, path: Path) -> dict:
    relative = path.relative_to(root).as_posix()
    digest = trusted_source_sha256(path, root=root, max_bytes=TEXT_BYTES)
    with bind_execution_scope(ExecutionScope(readable_roots=(root.resolve(),))):
        payload = read_bytes(path.absolute(), TEXT_BYTES + 1)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("task file changed during readback")
    return {"source": relative, "sha256": digest, "size_bytes": len(payload),
            "text": payload.decode("utf-8"), "readback": True}


def ordinary_paths(root: Path):
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [n for n in dirs if not n.startswith(".") and not (Path(directory) / n).is_symlink()]
        for name in files:
            if not name.startswith(".") and name != IDENTITY_FILENAME:
                yield Path(directory) / name


def read_commands(command: str) -> list[str]:
    """Recognize simple read commands, never execute or interpret shell programs."""
    try:
        outer = shlex.split(command)
        if len(outer) == 3 and Path(outer[0]).name in {"bash", "sh"} and outer[1] in {"-c", "-lc"}:
            command = outer[2]
        if any(token in command for token in ("$", "`", "\n")):
            return []
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        parts: list[list[str]] = [[]]
        for token in lexer:
            if token == "&&":
                parts.append([])
            elif token in {";", "|", "||", "&", ">", ">>", "<", "(", ")"}:
                return []
            else:
                parts[-1].append(token)
        paths = []
        for args in parts:
            if not args:
                return []
            name, tail = Path(args[0]).name, args[1:]
            if name == "pwd" and not tail:
                continue
            if name == "cat":
                tail = tail[1:] if tail[:1] == ["--"] else tail
            elif name == "sed" and len(tail) >= 3 and tail[0] == "-n" and re.fullmatch(r"\d+(?:,\d+)?p", tail[1]):
                tail = tail[2:]
            elif name == "head" and len(tail) >= 3 and tail[0] == "-n" and tail[1].isdigit():
                tail = tail[2:]
            elif name == "rg" and len(tail) >= 3 and tail[0] == "-n" and tail[1] in {".", "^"}:
                # Line-numbered full-text reads are also ordinary source evidence.
                tail = tail[2:]
            else:
                return []
            if not tail or any(p.startswith("-") or any(c in p for c in "*?[]") for p in tail):
                return []
            paths.extend(tail)
        return paths
    except ValueError:
        return []


class TaskFileEvidence:
    def __init__(self, root: Path):
        self.root = root.absolute()
        self.inputs = {}
        self.baseline = set()
        self.reads: list[dict] = []
        self.starts: dict[tuple, dict] = {}
        for path in ordinary_paths(self.root):
            self.baseline.add(path.relative_to(self.root).as_posix())
            try:
                self.inputs[path.relative_to(self.root).as_posix()] = file_record(self.root, path)
            except UnicodeError:
                # Invalid-document and image fixtures are not text evidence.
                continue

    def observe(self, event: dict) -> None:
        if event.get("kind") != "command" or event.get("source") != "provider":
            return
        key = (event.get("span_id"), event.get("turn_index"), event.get("actor"))
        if event.get("type") == "SpanStarted":
            self.starts[key] = event
            return
        if event.get("type") != "SpanFinished":
            return
        start = self.starts.pop(key, None)
        data = event.get("data") or {}
        output = data.get("output") or {}
        if not start or event.get("ok") is not True or output.get("exit_code") != 0:
            return
        request = (start.get("data") or {}).get("input") or {}
        if data.get("input") != request:
            return
        stdout = output.get("aggregated_output")
        if not isinstance(stdout, str):
            return
        cwd = Path(request.get("cwd") or self.root)
        for name in read_commands(str(request.get("command") or "")):
            path = Path(name)
            path = path if path.is_absolute() else cwd / path
            try:
                relative = path.relative_to(self.root).as_posix()
                source = self.inputs.get(relative)
                if not source or not source["text"].strip() or source["text"].strip() not in stdout:
                    continue
                if file_record(self.root, path)["sha256"] != source["sha256"]:
                    continue
            except (OSError, ValueError):
                continue
            self.reads.append({**source, "method": "native_command", "span_id": key[0],
                               "turn_index": key[1], "actor": key[2]})

    def snapshot(self) -> dict:
        artifacts, unreadable = [], []
        for path in ordinary_paths(self.root):
            relative = path.relative_to(self.root).as_posix()
            if relative in self.baseline:
                continue
            try:
                artifacts.append(file_record(self.root, path))
            except (OSError, ValueError) as exc:
                unreadable.append({"source": relative, "error": type(exc).__name__})
        unchanged = True
        for source, original in self.inputs.items():
            try:
                unchanged = unchanged and file_record(self.root, self.root / source)["sha256"] == original["sha256"]
            except (OSError, ValueError):
                unchanged = False
        return {"native_reads": list(self.reads), "artifacts": artifacts, "input_files_unchanged": unchanged,
                "artifact_read_errors": unreadable}


def quality_evidence(observation) -> dict:
    """Keep availability, actual reads and hidden fixture state distinct for Judge."""
    turns = [e for e in observation.evidence if e.get("kind") == "agent_turn_result"]
    return {
        "evidence_contract": "Only observed inputs/reads are Agent knowledge; fixture state is judge-only. "
        "A resource binding proves availability, not reading. A file readback proves existence, not delivery.",
        "turn_bindings": [{k: t.get(k) for k in ("turn_index", "conversation_id", "execution_session_id", "resources")}
                          for t in turns],
        "input_resources": [e for e in observation.evidence if e.get("kind") in {"input_resource", "input_resource_dispatch"}],
        "available_tools": [e for e in observation.evidence if e.get("kind") == "task_tools"],
        "file_observations": [e for e in observation.evidence if e.get("kind") == "task_file_evidence"],
        "tool_observations": list(observation.tool_calls),
    }
