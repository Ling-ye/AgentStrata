"""Consume independently admitted Windows runtime evidence; never issue or execute it."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re

from chatcopilot.core.install_artifacts import InstallError

CHECKS = frozenset({"input-integrity", "isolated-prefix", "entry-points", "native-loading-abi",
                    "application-runtime", "update-rollback"})


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def pin(value) -> str:
    if type(value) is not str or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise InstallError("Runtime evidence requires an exact lowercase SHA256")
    return value


def keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise InstallError("Runtime evidence has unsupported or missing fields")


def admitted(data, expected):
    if type(data) is not bytes or not 0 < len(data) <= 128 * 1024 or digest(data) != pin(expected):
        raise InstallError("Runtime evidence differs from its independent host pin or budget")
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise InstallError("Runtime evidence contains duplicate keys")
            result[name] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=unique)
    except (UnicodeError, ValueError, RecursionError):
        raise InstallError("Runtime evidence is not bounded valid JSON") from None
    if type(value) is not dict:
        raise InstallError("Runtime evidence requires a JSON object")
    return value


@dataclass(frozen=True)
class InstallationBinding:
    application_id: str
    install_id: str
    owner_sid: str
    installation_root_sha256: str
    data_root_sha256: str
    source_sha256: str
    artifacts_sha256: str
    launcher_sha256: str

    def __post_init__(self):
        if (type(self.application_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", self.application_id)
                or type(self.install_id) is not str or not re.fullmatch(r"[a-z][a-z0-9-]{1,80}", self.install_id)
                or type(self.owner_sid) is not str or len(self.owner_sid) > 100 or not re.fullmatch(r"S-1-5-21-(?:[1-9][0-9]*-){3}[1-9][0-9]*", self.owner_sid)
                or any(int(part) > 4294967295 for part in self.owner_sid.split("-")[4:])):
            raise InstallError("Runtime acceptance requires an explicit application, installation and owner SID")
        for value in (self.installation_root_sha256, self.data_root_sha256, self.source_sha256,
                      self.artifacts_sha256, self.launcher_sha256):
            pin(value)

    @property
    def identity_sha256(self):
        # Mutable business data contents and credentials are never hashed or copied.
        return digest(canonical({name: asdict(self)[name] for name in (
            "application_id", "install_id", "owner_sid", "installation_root_sha256", "data_root_sha256")}))


@dataclass(frozen=True)
class RuntimeAcceptance:
    receipt_sha256: str
    requirements_sha256: str
    binding: InstallationBinding
    scopes: tuple[str, ...]

    def __post_init__(self):
        pin(self.receipt_sha256)
        pin(self.requirements_sha256)
        if (type(self.binding) is not InstallationBinding or type(self.scopes) is not tuple
                or not 1 <= len(self.scopes) <= 64 or any(type(scope) is not str for scope in self.scopes)
                or tuple(sorted(set(self.scopes))) != self.scopes):
            raise InstallError("Admitted runtime facts require immutable validated scope identities")


def validate_runtime_acceptance(receipt: bytes, requirements: bytes, *, expected_receipt_sha256: str,
        expected_requirements_sha256: str, binding: InstallationBinding) -> RuntimeAcceptance:
    """Host-supplied pins and binding must come from independent admission, never local JSON.

    Hash verification authenticates only the admitted bytes. This function runs
    no checks itself, signs no acceptance and does not authorize installation.
    """
    declaration = admitted(requirements, expected_requirements_sha256)
    value = admitted(receipt, expected_receipt_sha256)
    if type(binding) is not InstallationBinding:
        raise InstallError("Runtime acceptance requires a trusted typed installation binding")
    keys(declaration, {"schema", "state", "source_digest", "application_id", "scopes"})
    if (declaration["schema"] != "agentstrata-windows-runtime-requirements/v1" or declaration["state"] != "complete"
            or declaration["source_digest"] != binding.source_sha256 or declaration["application_id"] != binding.application_id):
        raise InstallError("Runtime input requirements are incomplete or identify another application source")
    scopes = declaration["scopes"]
    if type(scopes) is not list or not 1 <= len(scopes) <= 64:
        raise InstallError("Runtime acceptance requires a bounded complete scope set")
    expected = {}
    for row in scopes:
        keys(row, {"id", "inputs_sha256", "inputs", "blockers"})
        scope = row["id"]
        if type(scope) is not str or not re.fullmatch(r"[a-z][a-z0-9:-]{0,80}", scope) or scope in expected:
            raise InstallError("Runtime scope names are unsafe or repeat")
        inputs = row["inputs"]
        if type(inputs) is not dict or not 1 <= len(inputs) <= 4096 or any(type(name) is not str or len(name) > 1024 for name in inputs):
            raise InstallError("Runtime scope requires a bounded immutable input map")
        for source_pin in inputs.values():
            pin(source_pin)
        if pin(row["inputs_sha256"]) != digest(canonical(inputs)) or type(row["blockers"]) is not list or row["blockers"]:
            raise InstallError("Runtime scope has missing inputs, blockers or a changed input digest")
        expected[scope] = row["inputs_sha256"]
    if not {"application", "host", "launcher", "mcp"}.issubset(expected):
        raise InstallError("Runtime acceptance cannot omit mandatory application/host/launcher/MCP scopes")
    keys(value, {"schema", "state", "binding", "requirements_sha256", "scopes", "checks", "independent_review_sha256"})
    if (value["schema"] != "agentstrata-windows-runtime-acceptance/v1" or value["state"] != "accepted"
            or value["binding"] != asdict(binding) or value["requirements_sha256"] != expected_requirements_sha256):
        raise InstallError("Runtime acceptance is not bound to this independent installation and input set")
    keys(value["checks"], CHECKS)
    for evidence in (*value["checks"].values(), value["independent_review_sha256"]):
        pin(evidence)
    actual = {}
    if type(value["scopes"]) is not list or len(value["scopes"]) != len(expected):
        raise InstallError("Runtime acceptance scope coverage is incomplete")
    for row in value["scopes"]:
        keys(row, {"id", "inputs_sha256", "plan_sha256", "filesystem_binding_sha256", "verification_sha256"})
        scope = row["id"]
        if type(scope) is not str or scope not in expected or scope in actual or row["inputs_sha256"] != expected[scope]:
            raise InstallError("Runtime acceptance scope differs from its frozen requirements")
        for name in ("plan_sha256", "filesystem_binding_sha256", "verification_sha256"):
            pin(row[name])
        actual[scope] = row["inputs_sha256"]
    return RuntimeAcceptance(expected_receipt_sha256, expected_requirements_sha256, binding, tuple(sorted(actual)))
