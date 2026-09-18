"""Declarative, content-addressed Agent cases; no executable user-supplied graders."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.evals.models import EvalCase

SCHEMA = "agentstrata.agent-case/v1"
SUITE = "agentstrata-regression-v1"
_FIELDS = {"schema", "title", "input", "context", "role", "channel_kind", "allowed_tools", "fixtures", "assertions", "expected_behavior", "semantic", "resources", "external_fixtures"}
_ASSERTIONS = {
    "final_contains": {"kind", "value"}, "final_not_contains": {"kind", "value"},
    "tool_called": {"kind", "name", "arguments"}, "tool_not_called": {"kind", "name"},
    "tool_result_contains": {"kind", "name", "value"},
    "file_equals": {"kind", "path", "value"}, "file_exists": {"kind", "path"},
    "image_delivered": {"kind"},
}
# These tools operate through the real product handlers and the trial workspace.
# External tools require a purpose-built dependency fixture before registration.
LOCAL_PACKS = ("workspace.read_write", "playbooks.reader")
# Environment policy, not discovery: these handlers need only the isolated
# workspace or packaged playbooks. Delivery, network and global-state tools
# require separate dependency fixtures and are intentionally unavailable here.
ISOLATED_TOOLS = frozenset({"read_text_head", "write_workspace_file", "list_workspace", "unzip_attachment", "read_bot_skill"})
IMAGE_TOOLS = frozenset({"download_image_urls", "send_image_urls_to_user", "send_files_to_user"})


def capabilities() -> dict[str, Any]:
    return {"schema": SCHEMA, "tools": sorted(ISOLATED_TOOLS | IMAGE_TOOLS), "fixtures": ["workspace", "input_image", "http", "delivery"],
            "external_network": False, "production_delivery": False,
            "case_fields": sorted(_FIELDS), "assertions": sorted(_ASSERTIONS)}


class CaseCapabilityUnavailable(ValueError):
    """A missing trusted dependency, distinct from an author-correctable definition."""
    code = "fixture_missing"


def relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("fixture path must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} or part.startswith(".") for part in path.parts) or str(path) != value:
        raise ValueError("fixture path escapes the ordinary workspace")
    return value


def validate_case(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _FIELDS or value.get("schema") != SCHEMA:
        raise ValueError("invalid frozen Agent Case schema")
    case = {"context": "", "role": "owner", "channel_kind": "private", "allowed_tools": [],
            "fixtures": {}, "assertions": [], "semantic": False, **value}
    for name in ("title", "input", "context", "expected_behavior"):
        if not isinstance(case.get(name), str) or (name != "context" and not case[name].strip()):
            raise ValueError(f"Agent Case {name} must be text")
    if case["role"] not in {"owner", "user", "admin"} or case["channel_kind"] not in {"private", "group"}:
        raise ValueError("invalid isolated actor")
    if type(case["semantic"]) is not bool:
        raise ValueError("semantic must be boolean")
    names = case["allowed_tools"]
    if not isinstance(names, list) or any(not isinstance(n, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", n) for n in names) or len(names) != len(set(names)):
        raise ValueError("invalid allowed tools")
    if "external_fixtures" in case:
        from chatcopilot.evals.image_delivery_fixture import validate_fixtures
        validate_fixtures(case["external_fixtures"])
    if set(names) - (ISOLATED_TOOLS | (IMAGE_TOOLS if case.get("external_fixtures") else frozenset())):
        raise CaseCapabilityUnavailable("required tool has no isolated dependency fixture")
    if not isinstance(case["fixtures"], dict):
        raise ValueError("fixtures must map relative paths to text")
    for path, content in case["fixtures"].items():
        relative_path(path)
        if not isinstance(content, str):
            raise ValueError("fixtures must contain text data, not executable hooks")
    checks = case["assertions"]
    if not isinstance(checks, list) or (not checks and not case["semantic"]):
        raise ValueError("Agent Case needs behavioral assertions or required semantic scoring")
    for check in checks:
        if not isinstance(check, dict) or check.get("kind") not in _ASSERTIONS:
            raise ValueError("untrusted Agent Case assertion")
        if check["kind"] == "image_delivered" and not case.get("external_fixtures"):
            raise CaseCapabilityUnavailable("image delivery assertion requires isolated dependency fixture")
        fields = _ASSERTIONS[check["kind"]]
        if set(check) - fields or fields - {"arguments"} - set(check):
            raise ValueError("invalid assertion fields")
        if "path" in check:
            relative_path(check["path"])
        if "name" in check and (not isinstance(check["name"], str) or not check["name"]):
            raise ValueError("assertion tool name must be text")
        if "value" in check and (not isinstance(check["value"], str) or not check["value"]):
            raise ValueError("assertion value must be nonempty text")
        if check["kind"] in {"tool_called", "tool_result_contains"} and check["name"] not in names:
            raise ValueError("required tool assertion is outside the frozen allowed tools")
        if "arguments" in check and not isinstance(check["arguments"], dict):
            raise ValueError("assertion arguments must be an object")
    if "resources" in case:
        from chatcopilot.evals.case_images import image_reference
        refs = case["resources"]
        if not isinstance(refs, list) or not refs:
            raise ValueError("Case resources must contain image references")
        case["resources"] = [image_reference(ref) for ref in refs]
        if len({ref["scope"] for ref in refs}) != 1 or len({ref["sha256"] for ref in refs}) != len(refs):
            raise ValueError("Case image scopes or identities are inconsistent")
    if len(json_text(case).encode()) > 512 * 1024:
        raise ValueError("Agent Case exceeds the service evidence boundary")
    return json.loads(json_text(case))


def case_identity(value: dict[str, Any]) -> str:
    return "snapshot-" + hashlib.sha256(json_text(validate_case(value)).encode()).hexdigest()


def evaluation_cases(snapshot: dict[str, Any]) -> tuple[EvalCase, ...]:
    case = validate_case(snapshot["case"])
    identity = case_identity(case)
    if snapshot.get("snapshot_id") != identity:
        raise ValueError("frozen Agent Case digest changed")
    return (EvalCase(case_id=identity, input=case["input"], category="agent_regression",
                     expected_behavior=case["expected_behavior"], context=case["context"],
                     metadata={"plugin": "frozen-agent", "driver": "agent_configured",
                               "agent_case": case, **({"image_root": snapshot["image_root"]} if snapshot.get("image_root") else {}), "case_source": {"kind": "agent_regression"}}),)


def load_regressions(repository: Path) -> tuple[dict[str, Any], ...]:
    """Only the host's repository loader adopts published declaration files."""
    from chatcopilot.core.file_integrity import require_regular_file
    root = repository.resolve() / "tests/agent_regressions"
    result = []
    for path in sorted(root.glob("*/case.json")):
        if path.resolve() != path or path.parent.parent != root:
            raise ValueError("Agent regression path changed")
        require_regular_file(path.lstat())
        case = validate_case(json.loads(path.read_text()))
        if case.get("resources"):
            raise ValueError("private Case images cannot be published as a regression")
        ident = case_identity(case)
        if path.parent.name != ident.removeprefix("snapshot-"):
            raise ValueError("Agent regression directory does not match content")
        result.append({"snapshot_id": ident, "case": case})
    return tuple(result)
