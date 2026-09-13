"""Private immutable local trace files; readers never create or repair storage."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
from typing import Any, Iterator

from chatcopilot.core.private_sqlite import json_text, private_directory
from chatcopilot.core.trace_capture import REF_KEY, LITERAL_KEY, TraceCapture, trace_id
from chatcopilot.core.trace_codec import validate_trace, encode_trace

_REF = re.compile(r"trace-[a-f0-9]{32}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
GROUPS = ("baseSpans", "agentSpans", "llmSpans", "toolSpans", "retrieverSpans")


def _directory(path: Path) -> None:
    current = Path(path.absolute().anchor)
    for part in path.absolute().parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("Trace directory contains a symlink")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Trace directory must be private and owned by the current user")


def _read(path: Path) -> bytes:
    _directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError("Trace file is not a private regular file")
        return stream.read()


def _write(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _json(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate trace JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite trace JSON")))


def metadata(trace: dict[str, Any]) -> dict[str, Any]:
    return trace["metadata"]["agentstrata"]


class TraceArchive:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def directory(self, ref: str) -> Path:
        if not _REF.fullmatch(ref):
            raise ValueError("Invalid trace reference")
        return self.root / ref

    def save(self, capture: TraceCapture, status: str, *, retained: bool = False) -> dict[str, Any]:
        trace = capture.finish(status, retained=retained)
        return self.publish(trace, capture.artifacts)

    def publish(self, trace: dict[str, Any], artifacts: dict[str, bytes]) -> dict[str, Any]:
        trace = encode_trace(trace)
        validate_trace(trace)
        meta = metadata(trace)
        if trace["uuid"] != trace_id(meta["source"]):
            raise ValueError("Trace source identity mismatch")
        if set(artifacts) != set(meta["artifacts"]):
            raise ValueError("Trace artifact manifest mismatch")
        private_directory(self.root)
        target = self.directory(trace["uuid"])
        # A terminal record is immutable, including on a retry after publishing.
        if target.exists() or target.is_symlink():
            previous = self.load(trace["uuid"], source=meta["source"])
            if json_text(previous) != json_text(trace):
                raise ValueError("Trace already exists with different contents")
            return self.reference(previous)
        temporary = Path(tempfile.mkdtemp(prefix=".trace-", dir=self.root))
        try:
            body_dir = private_directory(temporary / "artifacts")
            for digest, raw in artifacts.items():
                self._check_body(meta, digest, raw)
                _write(body_dir / f"{digest}.json", raw)
            _write(temporary / "trace.json", json_text(trace).encode())
            # Validate all references before making the record visible.
            self._validate_refs(trace, artifacts)
            os.rename(temporary, target)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return self.reference(trace)

    @staticmethod
    def reference(trace: dict[str, Any]) -> dict[str, Any]:
        meta = metadata(trace)
        return {"trace_ref": trace["uuid"], "sha256": hashlib.sha256(json_text(trace).encode()).hexdigest(),
                "capture_state": meta["capture_state"], "source": meta["source"],
                "finished_at": meta["finished_at"], "expires_at": meta["expires_at"]}

    def load(self, ref: str, *, source: dict[str, Any] | None = None,
             sha256: str | None = None) -> dict[str, Any]:
        _directory(self.root)
        raw = _read(self.directory(ref) / "trace.json")
        if sha256 and hashlib.sha256(raw).hexdigest() != sha256:
            raise ValueError("Trace index digest changed")
        value = _json(raw)
        validate_trace(value)
        if value["uuid"] != ref or trace_id(metadata(value)["source"]) != ref:
            raise ValueError("Trace binding mismatch")
        if source is not None and metadata(value)["source"] != source:
            raise ValueError("Trace belongs to another execution")
        return value

    @staticmethod
    def _check_body(meta: dict[str, Any], digest: str, raw: bytes) -> None:
        if (not _HASH.fullmatch(digest) or meta["artifacts"].get(digest) != len(raw)
                or hashlib.sha256(raw).hexdigest() != digest):
            raise ValueError("Trace artifact is missing or its content changed")

    def _body(self, trace: dict[str, Any], digest: str) -> Any:
        meta = metadata(trace)
        if not _HASH.fullmatch(digest) or digest not in meta["artifacts"]:
            raise ValueError("Artifact does not belong to this trace")
        if meta["expires_at"] is not None and meta["expires_at"] <= time.time():
            raise ValueError("Trace body expired")
        raw = _read(self.directory(trace["uuid"]) / "artifacts" / f"{digest}.json")
        self._check_body(meta, digest, raw)
        return _json(raw)

    def resolve(self, trace: dict[str, Any], value: Any) -> Any:
        return self._resolve(value, lambda digest: self._body(trace, digest), set())

    @staticmethod
    def _resolve(value: Any, reader: Any, ancestors: set[str], *, literal: bool = False) -> Any:
        if isinstance(value, dict):
            if not literal and set(value) == {LITERAL_KEY}:
                return TraceArchive._resolve(value[LITERAL_KEY], reader, ancestors, literal=True)
            if not literal and REF_KEY in value:
                digest = value[REF_KEY]
                if set(value) != {REF_KEY, "bytes"} or not isinstance(digest, str) or digest in ancestors:
                    raise ValueError("Invalid or cyclic trace reference")
                resolved = reader(digest)
                if value["bytes"] != len(json_text(resolved).encode()):
                    raise ValueError("Trace reference size mismatch")
                return TraceArchive._resolve(resolved, reader, ancestors | {digest})
            return {key: TraceArchive._resolve(item, reader, ancestors) for key, item in value.items()}
        if isinstance(value, list):
            return [TraceArchive._resolve(item, reader, ancestors) for item in value]
        return value

    @staticmethod
    def _validate_refs(trace: dict[str, Any], artifacts: dict[str, bytes]) -> None:
        def reader(digest: str) -> Any:
            if digest not in artifacts:
                raise ValueError("Trace contains an unbound artifact")
            return _json(artifacts[digest])
        TraceArchive._resolve({key: trace.get(key) for key in GROUPS}, reader, set())

    def summary(self, ref: str, *, after: int = 0, limit: int = 200,
                source: dict[str, Any] | None = None, sha256: str | None = None) -> dict[str, Any]:
        if after < 0 or not 1 <= limit <= 500:
            raise ValueError("Invalid trace page")
        trace = self.load(ref, source=source, sha256=sha256)
        meta = metadata(trace)
        spans = sorted((span for group in GROUPS for span in trace.get(group, [])),
                       key=lambda span: span["metadata"]["agentstrata"]["order"])
        rows = []
        for span in spans[after:after + limit]:
            data = span["metadata"]["agentstrata"]
            rows.append({"id": span["uuid"], "parent_id": span.get("parentUuid"), "name": span["name"],
                         "type": span["type"], "status": data["status"], "start_time": span["startTime"],
                         "end_time": span["endTime"] if data.get("end_observed", True) else None,
                         "model": span.get("model"), "order": data["order"],
                         "event_count": len(data["events"]),
                         "coverage": next((e["data"].get("coverage") for e in data["events"] if e["data"].get("coverage")), None)})
        expired = meta["expires_at"] is not None and meta["expires_at"] <= time.time()
        return {**self.reference(trace), "capture_state": "expired" if expired else meta["capture_state"],
                "execution_status": meta["execution_status"], "capture_reasons": meta["capture_reasons"],
                "spans": rows, "next_cursor": after + len(rows), "has_more": after + len(rows) < len(spans),
                "span_count": len(spans), "sdk_version": meta["sdk_version"]}

    def step(self, ref: str, span_id: str, *, source: dict[str, Any] | None = None,
             sha256: str | None = None) -> dict[str, Any]:
        trace = self.load(ref, source=source, sha256=sha256)
        for group in GROUPS:
            for span in trace.get(group, []):
                if span["uuid"] == span_id:
                    selected = {key: value for key, value in span.items() if key != "metadata"}
                    meta = span["metadata"]["agentstrata"]
                    selected["metadata"] = {"agentstrata": {**meta, "events": [
                        {key: value for key, value in event.items() if key != "body"} for event in meta["events"]]}}
                    return self.resolve(trace, selected)
        raise ValueError("Trace step not found")

    def export(self, ref: str, *, source: dict[str, Any] | None = None,
               sha256: str | None = None) -> dict[str, Any]:
        trace = self.load(ref, source=source, sha256=sha256)
        artifacts = {}
        for digest in metadata(trace)["artifacts"]:
            self._body(trace, digest)
            artifacts[digest] = _read(self.directory(ref) / "artifacts" / f"{digest}.json").decode()
        return {"trace": trace, "artifacts": artifacts}

    def freeze(self, bundle: dict[str, Any]) -> dict[str, Any]:
        trace = _json(json_text(bundle["trace"]).encode())
        metadata(trace)["expires_at"] = None
        return self.publish(trace, {key: raw.encode() for key, raw in bundle["artifacts"].items()})

    def expire(self, ref: str) -> None:
        trace = self.load(ref)
        meta = metadata(trace)
        if meta["expires_at"] is None or meta["expires_at"] > time.time():
            return
        body_dir = self.directory(ref) / "artifacts"
        _directory(body_dir)
        for digest in meta["artifacts"]:
            if not _HASH.fullmatch(digest):
                raise ValueError("Invalid artifact manifest")
            path = body_dir / f"{digest}.json"
            if path.exists() or path.is_symlink():
                _read(path)
                path.unlink()

    def expire_all(self) -> None:
        if not self.root.exists():
            return
        _directory(self.root)
        for path in self.root.iterdir():
            if _REF.fullmatch(path.name):
                self.expire(path.name)

    def portable_events(self, ref: str) -> Iterator[dict[str, Any]]:
        trace = self.load(ref)
        events = [event for group in GROUPS for span in trace.get(group, [])
                  for event in span["metadata"]["agentstrata"]["events"]]
        for event in sorted(events, key=lambda item: item["seq"]):
            yield self.resolve(trace, event)
