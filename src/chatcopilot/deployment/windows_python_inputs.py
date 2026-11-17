"""Resolve frozen Python supplier inputs in production payloads, without downloading or executing."""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re

from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.python_plan import resolve_python_inputs
from chatcopilot.core.source_snapshot import manifest_digest

LOCK = "integrations/live2d_desktop/windows-python-inputs.lock.json"
RUNTIME = "integrations/live2d_desktop/windows-runtime.lock.json"
MCP = "integrations/live2d_desktop/windows-mcp.lock.json"
METADATA = "windows-python-download-metadata.json"
MEMBER = "windows-python-input-verification.json"
_V2 = "agentstrata-windows-python-inputs/v2"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def _json(raw: bytes) -> dict:
    def unique(pairs):
        value = {}
        for name, item in pairs:
            if name in value:
                raise ValueError("Duplicate native Python source key")
            value[name] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("Malformed native Python source input") from None
    if type(value) is not dict:
        raise ValueError("Native Python source input requires an object")
    return value


def _read(path: Path, root: Path, limit: int, expected: str | None = None) -> bytes:
    observed = trusted_source_sha256(path, root=root, max_bytes=limit)
    with path.open("rb") as stream:
        raw = stream.read(limit+1)
    if len(raw) > limit or _sha(raw) != observed or expected is not None and observed != expected:
        raise ValueError("Native Python input differs from its frozen bytes or budget")
    return raw


def _frozen(payload: Path, manifest: dict, name: str, limit: int = 64*1024) -> bytes:
    if name not in manifest:
        raise ValueError("Native Python input requires its frozen source declaration")
    return _read(payload/"source"/name, payload/"source", limit, manifest[name]["sha256"])


def _report(payload: Path, manifest: dict, raw: bytes, metadata: bytes) -> bytes:
    # Authenticating source consistency is separate from independent release admission.
    resolved = resolve_python_inputs(raw, metadata, expected_inputs_sha256=manifest[LOCK]["sha256"])
    runtime, mcp = _frozen(payload, manifest, RUNTIME), _frozen(payload, manifest, MCP)
    versions = _json(runtime)
    archives = {item.lane:asdict(item) for item in resolved.archives}
    if (archives["host_python"]["version"] != versions.get("host_python")
            or archives["vtuber_python"]["version"] != versions.get("vtuber_python")):
        raise ValueError("Frozen Python inputs differ from host/plugin runtime declarations")
    if "windows_mcp_python" in archives and archives["windows_mcp_python"]["version"] != _json(mcp).get("python"):
        raise ValueError("Frozen Windows-MCP Python input differs from its declared executor")
    return _canonical({"schema":"agentstrata-windows-python-input-verification/v1", "state":"resolved-inputs",
        "source_digest":manifest_digest(manifest),
        "source_inputs":{LOCK:_sha(raw), RUNTIME:_sha(runtime), MCP:_sha(mcp)},
        "input_lock_sha256":resolved.inputs_sha256,"metadata_sha256":resolved.metadata_sha256,
        "archives":archives,"archive_bytes_audited":False,"wheel_closure_verified":False,
        "runtime_verified":False,"installation_accepted":False})


def prepare_native_python_inputs(payload: Path, manifest: dict, *, metadata: Path | None = None,
                                metadata_cache: Path | None = None) -> dict[str, str]:
    """Use explicit supplier bytes or a hash-addressed host cache, never an installed environment."""
    if LOCK not in manifest:
        if metadata is not None:
            raise ValueError("Native Python metadata has no frozen source lock")
        return {}
    raw = _frozen(payload, manifest, LOCK)
    document = _json(raw)
    if metadata is None:
        if document.get("schema") == "agentstrata-windows-python-inputs/v1":
            return {}  # Preserve readable legacy source payloads; never promote their trust.
        source = document.get("source")
        expected = source.get("sha256") if type(source) is dict else None
        if type(expected) is not str or re.fullmatch(r"[a-f0-9]{64}",expected) is None:
            raise ValueError("Native Python supplier cache requires a canonical frozen SHA256")
        directory = metadata_cache if metadata_cache is not None else Path.home()/".cache/agentstrata/python-inputs"
        metadata = directory/(expected+".json")
        if not metadata.exists():
            raise ValueError("Native Python v2 requires explicit frozen supplier metadata or its hash-addressed host cache; no download or environment fallback")
    table = _read(metadata, metadata.parent, 8*1024*1024)
    report = _report(payload, manifest, raw, table)
    for name, content in ((METADATA,table),(MEMBER,report)):
        with (payload/name).open("xb") as stream:
            stream.write(content)
    return {METADATA:_sha(table), MEMBER:_sha(report)}


def verify_native_python_inputs(payload: Path, manifest: dict, members: Collection[str] | None = None) -> dict | None:
    """Recompute all declared facts from original source and metadata, including on artifact rehash."""
    if members is None:
        members = {name for name in (MEMBER,METADATA) if (payload/name).exists() or (payload/name).is_symlink()}
    present = {name for name in (MEMBER,METADATA) if name in members}
    if LOCK not in manifest:
        if present:
            raise ValueError("Native Python artifacts lack their frozen source lock")
        return None
    raw = _frozen(payload,manifest,LOCK)
    schema = _json(raw).get("schema")
    if schema not in ("agentstrata-windows-python-inputs/v1",_V2):
        raise ValueError("Unsupported native Python source input schema")
    if not present and schema != _V2:
        return None
    if present != {MEMBER,METADATA}:
        raise ValueError("Native Python v2 source requires both original metadata and input verification artifacts")
    table = _read(payload/METADATA,payload,8*1024*1024)
    expected = _report(payload,manifest,raw,table)
    actual = _read(payload/MEMBER,payload,64*1024)
    if actual != expected:
        raise ValueError("Native Python input verification differs from its frozen source and supplier table")
    return _json(expected)
