"""Synthetic admitted evidence is never real runtime acceptance."""
from dataclasses import asdict, replace
import json
import subprocess

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.deployment.runtime_acceptance import CHECKS, InstallationBinding, canonical, digest, validate_runtime_acceptance
from chatcopilot.deployment import runtime_requirements as requirements


def h(value):
    return digest(value.encode())


def documents(version="a"):
    binding = InstallationBinding("demo", "demo-install", "S-1-5-21-1-2-3-1001", h("installation-root"), h("data-root"),
        h("source-"+version), h("artifacts-"+version), h("launcher-"+version))
    inputs = {"fixture-input": h("inert-"+version)}
    scopes = [{"id": name, "inputs":inputs, "inputs_sha256":digest(canonical(inputs)), "blockers":[]}
              for name in ("application", "host", "launcher", "mcp", "plugin:demo")]
    declaration = {"schema":"agentstrata-windows-runtime-requirements/v1", "state":"complete",
        "source_digest":binding.source_sha256, "application_id":"demo", "scopes":scopes}
    raw = canonical(declaration)
    receipt = {"schema":"agentstrata-windows-runtime-acceptance/v1", "state":"accepted", "binding":asdict(binding),
        "requirements_sha256":digest(raw), "checks":{name:h("synthetic-"+name) for name in CHECKS},
        "independent_review_sha256":h("synthetic independent review"),
        "scopes":[{"id":row["id"], "inputs_sha256":row["inputs_sha256"], "plan_sha256":h("plan"),
                   "filesystem_binding_sha256":h("storage"), "verification_sha256":h("verification")} for row in scopes]}
    return canonical(receipt), raw, binding


def admit(values=None):
    receipt, declaration, binding = values or documents()
    return validate_runtime_acceptance(receipt, declaration, expected_receipt_sha256=digest(receipt),
        expected_requirements_sha256=digest(declaration), binding=binding)


def changed(values, index, mutation):
    receipt, declaration, binding = values
    value = json.loads((receipt, declaration)[index])
    mutation(value)
    if index == 0:
        return canonical(value), declaration, binding
    declaration = canonical(value)
    receipt = json.loads(receipt)
    receipt["requirements_sha256"] = digest(declaration)
    return canonical(receipt), declaration, binding


def test_admitted_synthetic_evidence_binds_every_scope_and_installation_without_execution(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("acceptance reader executed code"))
    values = documents()
    result = admit(values)
    assert result.receipt_sha256 == digest(values[0]) and result.requirements_sha256 == digest(values[1])
    assert result.binding == values[2] and result.scopes == ("application", "host", "launcher", "mcp", "plugin:demo")
    assert result.binding.identity_sha256 == replace(result.binding, source_sha256=h("new-source")).identity_sha256
    assert result.binding.identity_sha256 != replace(result.binding, data_root_sha256=h("other-data")).identity_sha256


@pytest.mark.parametrize("index", [0, 1])
def test_independent_pin_precedes_any_json_interpretation(index):
    values = list(documents())
    pins = [digest(raw) for raw in values[:2]]
    values[index] = b"not JSON"
    with pytest.raises(InstallError, match="independent host pin"):
        validate_runtime_acceptance(*values[:2], binding=values[2], expected_receipt_sha256=pins[0], expected_requirements_sha256=pins[1])


@pytest.mark.parametrize("mutation", [
    lambda d:d.update(schema="unknown"), lambda d:d.update(state="staged"), lambda d:d.update(extra=True),
    lambda d:d["binding"].update(owner_sid="S-1-5-21-4-5-6-1001"), lambda d:d["binding"].update(source_sha256=h("other")),
    lambda d:d["binding"].update(launcher_sha256=h("other")), lambda d:d.update(requirements_sha256=h("other")),
    lambda d:d["scopes"].pop(), lambda d:d["scopes"].append(d["scopes"][0]),
    lambda d:d["scopes"][0].update(id="unexpected"), lambda d:d["scopes"][0].update(inputs_sha256=h("other")),
    lambda d:d["scopes"][0].update(plan_sha256=True), lambda d:d["scopes"][0].pop("filesystem_binding_sha256"),
    lambda d:d["checks"].pop("native-loading-abi"), lambda d:d["checks"].update(extra=h("other")),
    lambda d:d["checks"].update({"entry-points":False}), lambda d:d.update(independent_review_sha256=None),
], ids=["schema", "staged", "extra", "owner", "source", "launcher", "requirements", "missing-scope", "duplicate-scope",
         "foreign-scope", "scope-input", "boolean-plan", "missing-storage", "missing-abi", "extra-check", "false-check", "review"])
def test_even_admitted_receipts_cannot_omit_or_substitute_required_bindings(mutation):
    with pytest.raises(InstallError):
        admit(changed(documents(), 0, mutation))


@pytest.mark.parametrize("mutation", [
    lambda d:d.update(schema="unknown"), lambda d:d.update(state="incomplete"), lambda d:d.update(source_digest=h("other")),
    lambda d:d.update(application_id="another"), lambda d:d.update(scopes=[]),
    lambda d:d["scopes"][0].update(blockers=["missing-real-abi"]), lambda d:d["scopes"][0].update(inputs_sha256=h("other")),
    lambda d:d["scopes"][0].update(inputs={"fixture-input":True}), lambda d:d["scopes"].append(d["scopes"][0]),
    lambda d:d["scopes"].pop(3), lambda d:d["scopes"][0].update(id="../escape"), lambda d:d.update(extra="unsupported"),
], ids=["schema", "incomplete", "source", "application", "empty", "blocker", "input-digest", "input-type", "duplicate", "mandatory-mcp", "unsafe-scope", "extra"])
def test_requirements_cannot_promote_incomplete_plugin_or_mcp_inputs(mutation):
    with pytest.raises(InstallError):
        admit(changed(documents(), 1, mutation))


@pytest.mark.parametrize("raw", [b'{"schema":1,"schema":2}', b"[]", b"{"+b" "*(128*1024)+b"}", b'"invalid"', b'\xff'],
    ids=["duplicate", "array", "oversize", "string", "utf8"])
def test_ambiguous_or_unbounded_admitted_json_is_rejected(raw):
    _receipt, declaration, binding = documents()
    with pytest.raises(InstallError):
        validate_runtime_acceptance(raw, declaration, expected_receipt_sha256=digest(raw),
            expected_requirements_sha256=digest(declaration), binding=binding)


@pytest.mark.parametrize("field,value", [("owner_sid", "S-1-5-21-0-2-3-1"), ("owner_sid", "S-1-5-21-4294967296-2-3-1"),
    ("owner_sid", "S-1-5-21-"+"9"*5000+"-2-3-1"), ("install_id", "../escape"), ("source_sha256", "A"*64), ("data_root_sha256", None)],
    ids=["sid-zero", "sid-overflow", "sid-budget", "install-path", "hash-case", "missing-data"])
def test_installation_binding_rejects_ambiguous_sid_and_mutable_inputs(field, value):
    with pytest.raises(InstallError):
        replace(documents()[2], **{field:value})


@pytest.fixture
def package(tmp_path):
    payload = tmp_path / "payload"
    inputs = {"bots/demo/bot.yaml":b"id: demo\n", "bots/demo/components.yaml":b"plugins: [demo]\n",
        "bots/demo/mcp/servers.yaml":b"servers: []\n", "pyproject.toml":b"[project]\nname='demo'\n",
        "integrations/live2d_desktop/windows-mcp.lock.json":b'{"python":"3.14.7"}',
        "integrations/open_llm_vtuber/adapter.py":b"raise RuntimeError('inert source never executed')\n",
        "integrations/live2d_generator/agentstrata_live2d/resources/gpt-sovits.lock.json":b'{"revision":"fixture"}'}
    for name, raw in inputs.items():
        path = payload / "source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    manifest = {name:{"sha256":digest(raw), "executable":False} for name,raw in inputs.items()}
    return payload, manifest


def test_source_compiler_retains_plugin_mcp_blockers_and_never_issues_acceptance(package):
    payload, manifest = package
    rows = requirements.prepare_runtime_requirements(payload, manifest, "bots/demo/bot.yaml", "demo")
    raw = (payload/requirements.MEMBER).read_bytes()
    value = json.loads(raw)
    assert rows == {requirements.MEMBER:digest(raw)} and value["state"] == "incomplete"
    scopes = {row["id"]:row for row in value["scopes"]}
    assert {"application", "host", "launcher", "mcp", "tools", "plugin-components", "mcp:windows-mcp"} == set(scopes)
    assert "mcp-python-archive-not-pinned" in scopes["mcp:windows-mcp"]["blockers"]
    assert "exact-host-wheel-set-absent" in scopes["host"]["blockers"]
    plugin_inputs = scopes["plugin-components"]["inputs"]
    for name in ("integrations/open_llm_vtuber/adapter.py",
                 "integrations/live2d_generator/agentstrata_live2d/resources/gpt-sovits.lock.json"):
        assert plugin_inputs[name] == manifest[name]["sha256"]
    requirements.verify_runtime_requirements(payload, manifest, "bots/demo/bot.yaml", "demo")
    receipt, _declaration, binding = documents()
    with pytest.raises(InstallError, match="incomplete"):
        validate_runtime_acceptance(receipt, raw, expected_receipt_sha256=digest(receipt), expected_requirements_sha256=digest(raw), binding=binding)


@pytest.mark.parametrize("attack", ["remove-mcp", "promote", "change-source", "oversize-host"])
def test_rehashing_payload_cannot_remove_source_scope_blockers(package, attack):
    payload, manifest = package
    requirements.prepare_runtime_requirements(payload, manifest, "bots/demo/bot.yaml", "demo")
    raw = json.loads((payload/requirements.MEMBER).read_bytes())
    if attack == "remove-mcp":
        raw["scopes"] = [row for row in raw["scopes"] if row["id"] != "mcp:windows-mcp"]
    elif attack == "promote":
        raw["state"] = "complete"
    elif attack == "change-source":
        (payload/"source/bots/demo/components.yaml").write_bytes(b"plugins: []\n")
    else:
        (payload/requirements.ENVIRONMENT_INPUTS).write_bytes(b"x"*(64*1024+1))
    (payload/requirements.MEMBER).write_bytes(canonical(raw))
    with pytest.raises((ValueError, RuntimeError)):
        requirements.verify_runtime_requirements(payload, manifest, "bots/demo/bot.yaml", "demo")


def test_runtime_subject_preserves_explicit_application_identity_instead_of_folder_name(package):
    payload, manifest = package
    value = json.loads(requirements.build_runtime_requirements(payload, manifest, "bots/demo/bot.yaml", "custom-instance"))
    assert value["application_id"] == "custom-instance"
