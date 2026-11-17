"""Production source consistency and inert archive staging never issue runtime acceptance."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.source_snapshot import manifest_digest
from chatcopilot.deployment import windows_python_inputs as module
from chatcopilot.deployment.application_bundle import MANIFEST, NATIVE_SCHEMA, read_bundle
from chatcopilot.deployment.application_runtime import install_bundle
from chatcopilot.deployment.runtime_requirements import MEMBER as REQUIREMENTS, prepare_runtime_requirements
from tests.unit.test_python_plan import digest, encoded, v2_fixture


@pytest.fixture
def payload(tmp_path,monkeypatch):
    monkeypatch.setattr(subprocess,"Popen",lambda *_a,**_k:pytest.fail("Input verification executed a program"))
    root = tmp_path/"payload"
    lock,metadata,_archives = v2_fixture()
    inputs = {
        module.LOCK:lock,
        module.RUNTIME:encoded({"host_python":"3.13.15","vtuber_python":"3.12.14"}),
        module.MCP:encoded({"python":"3.14.7","revision":"b"*40,"archive_sha256":"c"*64}),
        "integrations/live2d_desktop/windows-host-requirements.lock":b"# inert source\n",
        "integrations/live2d_desktop/scripts/bootstrap-windows.ps1":b"throw 'inert'\n",
        "integrations/open_llm_vtuber/agentstrata_vtuber/resources/requirements-windows.lock":b"# inert plugin roots\n",
        "bots/demo/bot.yaml":b"id: demo\nagent_profile: agent.yaml\ngateway: {}\n",
        "bots/demo/agent.yaml":b"{}\n","bots/demo/components.yaml":b"plugins: [{id: open-llm-vtuber}]\n",
        "pyproject.toml":b"[project]\nname='inert-fixture'\n","uv.lock":b"# inert fixture\n",
        "src/chatcopilot/__main__.py":b"raise RuntimeError('inert')\n",
        "src/chatcopilot/application_cli.py":b"raise RuntimeError('inert')\n",
        "src/chatcopilot/deployment/windows_runtime.py":b"raise RuntimeError('inert')\n",
        "deploy/wsl/install_wsl_env.sh":b"exit 73\n","deploy/wsl/update_instance.sh":b"exit 73\n",
    }
    for name,raw in inputs.items():
        path=root/"source"/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(raw)
    manifest={name:{"sha256":digest(raw),"executable":False} for name,raw in inputs.items()}
    table=tmp_path/"original-supplier.json"
    table.write_bytes(metadata)
    return root,manifest,table


def package(root,manifest):
    files={path.name:digest(path.read_bytes()) for path in root.iterdir() if path.is_file() and path.name not in {MANIFEST,"artifacts.json"}}
    source_digest=manifest_digest(manifest)
    catalog=encoded({"source_digest":source_digest,"files":files})
    (root/"artifacts.json").write_bytes(catalog)
    (root/MANIFEST).write_bytes(encoded({"schema":NATIVE_SCHEMA,"platform":"windows-x86_64",
        "origin":{"kind":"source-build","base_revision":"a"*40},"bot_id":"demo","bot_relative":"bots/demo/bot.yaml",
        "source_manifest":manifest,"source_digest":source_digest,"artifacts_digest":digest(catalog)}))


def prepare(payload):
    root,manifest,table=payload
    return module.prepare_native_python_inputs(root,manifest,metadata=table)


def test_production_reader_and_scope_compiler_rederive_all_executor_inputs_and_keep_runtime_blocked(payload):
    root,manifest,_table=payload
    rows=prepare(payload)
    assert set(rows)=={module.MEMBER,module.METADATA}
    report=module.verify_native_python_inputs(root,manifest,rows)
    assert report["state"]=="resolved-inputs"
    assert set(report["archives"])=={"host_python","vtuber_python","windows_mcp_python"}
    assert report["archives"]["windows_mcp_python"]["version"]=="3.14.7"
    assert not any(report[key] for key in ("archive_bytes_audited","wheel_closure_verified","runtime_verified","installation_accepted"))
    prepare_runtime_requirements(root,manifest,"bots/demo/bot.yaml","demo")
    requirements=json.loads((root/REQUIREMENTS).read_bytes())
    assert requirements["state"]=="incomplete"
    scopes={row["id"]:row for row in requirements["scopes"]}
    for scope in ("host","plugin-components","mcp:windows-mcp"):
        assert all(scopes[scope]["inputs"][name]==pin for name,pin in rows.items())
    assert "mcp-python-archive-not-audited" in scopes["mcp:windows-mcp"]["blockers"]
    assert "mcp-python-archive-not-pinned" not in scopes["mcp:windows-mcp"]["blockers"]
    assert "mcp-complete-wheel-set-and-layout-absent" in scopes["mcp:windows-mcp"]["blockers"]
    package(root,manifest)
    assert read_bundle(root)["bot_id"]=="demo"


@pytest.mark.parametrize("missing", [{module.MEMBER},{module.METADATA},{module.MEMBER,module.METADATA}])
def test_rehashing_catalog_cannot_strip_new_source_required_inputs_before_actual_install_entry(payload,tmp_path,missing):
    root,manifest,_table=payload
    prepare(payload)
    for name in missing:
        (root/name).unlink()
    package(root,manifest)
    destination=tmp_path/"must-not-be-created"
    with pytest.raises(ValueError,match="requires both"):
        install_bundle(root,destination)
    assert not destination.exists()


@pytest.mark.parametrize("change", ["mcp-pin","host-version","source","state","accepted","abi","extra","duplicate","noncanonical"])
def test_rehashing_artifacts_cannot_substitute_derived_python_facts(payload,change):
    root,manifest,_table=payload
    prepare(payload)
    path=root/module.MEMBER
    value=json.loads(path.read_bytes())
    if change=="mcp-pin":
        value["archives"]["windows_mcp_python"]["sha256"]="1"*64
    elif change=="host-version":
        value["archives"]["host_python"]["version"]="3.14.7"
    elif change=="source":
        value["source_digest"]="1"*64
    elif change=="state":
        value["state"]="accepted"
    elif change=="accepted":
        value["installation_accepted"]=True
    elif change=="abi":
        value["runtime_verified"]=True
    elif change=="extra":
        value["ready"]=True
    raw=encoded(value)+b"\n"
    if change=="duplicate":
        raw=b'{"state":"accepted",'+raw[1:]
    elif change=="noncanonical":
        raw+=b" "
    path.write_bytes(raw)
    package(root,manifest)
    with pytest.raises(ValueError,match="verification differs"):
        read_bundle(root)


def test_catalog_rehash_cannot_replace_original_supplier_table_with_environment_facts(payload):
    root,manifest,_table=payload
    prepare(payload)
    (root/module.METADATA).write_bytes(b'{"installed-python":"3.14.7"}')
    package(root,manifest)
    with pytest.raises(InstallError,match="metadata differs"):
        read_bundle(root)


def test_new_input_report_cannot_replace_required_runtime_scopes_at_the_install_entry(payload,tmp_path):
    root,manifest,_table=payload
    prepare(payload)
    package(root,manifest)
    destination=tmp_path/"no-source-or-data-write"
    with pytest.raises(ValueError,match="requires source-derived runtime requirements"):
        install_bundle(root,destination)
    assert not destination.exists()


@pytest.mark.parametrize("name,version", [(module.RUNTIME,"3.12.99"),(module.MCP,"3.14.8")])
def test_changed_frozen_executor_version_must_match_same_supplier_input_not_just_new_source_digest(payload,name,version):
    root,manifest,_table=payload
    prepare(payload)
    path=root/"source"/name
    value=json.loads(path.read_bytes())
    value["host_python" if name==module.RUNTIME else "python"]=version
    path.write_bytes(encoded(value))
    manifest[name]["sha256"]=digest(path.read_bytes())
    package(root,manifest)
    with pytest.raises(ValueError,match="differ"):
        read_bundle(root)


def test_missing_metadata_has_no_installed_environment_or_network_fallback_and_writes_no_artifacts(payload):
    root,manifest,_table=payload
    with pytest.raises(ValueError,match="explicit frozen supplier metadata"):
        module.prepare_native_python_inputs(root,manifest,metadata_cache=root/"empty-host-cache")
    assert not (root/module.MEMBER).exists() and not (root/module.METADATA).exists()


def test_hash_addressed_original_supplier_cache_keeps_production_builds_offline(payload,tmp_path):
    root,manifest,table=payload
    cache=tmp_path/"host-cache"
    cache.mkdir()
    path=cache/(digest(table.read_bytes())+".json")
    path.write_bytes(table.read_bytes())
    rows=module.prepare_native_python_inputs(root,manifest,metadata_cache=cache)
    report=module.verify_native_python_inputs(root,manifest,rows)
    assert report["metadata_sha256"]==rows[module.METADATA]
    prepare_runtime_requirements(root,manifest,"bots/demo/bot.yaml","demo")
    package(root,manifest)
    assert read_bundle(root)["bot_id"]=="demo"


def test_hash_addressed_host_cache_is_not_a_provenance_root(payload,tmp_path):
    root,manifest,table=payload
    cache=tmp_path/"host-cache"
    cache.mkdir()
    path=cache/(digest(table.read_bytes())+".json")
    path.write_bytes(b"installed environment snapshot")
    with pytest.raises(InstallError,match="metadata differs"):
        module.prepare_native_python_inputs(root,manifest,metadata_cache=cache)
    assert not (root/module.MEMBER).exists()


@pytest.mark.parametrize("value", ["../fixture-secret","A"*64,True,None])
def test_supplier_digest_cannot_escape_the_host_cache_or_select_a_local_snapshot(payload,tmp_path,value):
    root,manifest,_table=payload
    path=root/"source"/module.LOCK
    document=json.loads(path.read_bytes())
    document["source"]["sha256"]=value
    path.write_bytes(encoded(document))
    manifest[module.LOCK]["sha256"]=digest(path.read_bytes())
    with pytest.raises(ValueError,match="canonical frozen SHA256"):
        module.prepare_native_python_inputs(root,manifest,metadata_cache=tmp_path/"host-cache")
    assert not (root/module.MEMBER).exists()


def test_supplier_integrity_precedes_any_artifact_write_and_existing_files_are_never_overwritten(payload):
    root,manifest,table=payload
    original=table.read_bytes()
    table.write_bytes(b"not supplier metadata")
    with pytest.raises(InstallError,match="metadata differs"):
        prepare(payload)
    assert not (root/module.MEMBER).exists() and not (root/module.METADATA).exists()
    table.write_bytes(original)
    (root/module.METADATA).write_bytes(b"preserved unrelated bytes")
    with pytest.raises(FileExistsError):
        prepare(payload)
    assert (root/module.METADATA).read_bytes()==b"preserved unrelated bytes"


def test_legacy_v1_payload_remains_readable_without_claiming_mcp_or_runtime_acceptance(payload):
    root,manifest,_table=payload
    path=root/"source"/module.LOCK
    value=json.loads(path.read_bytes())
    value["schema"]="agentstrata-windows-python-inputs/v1"
    value["archives"].pop("windows_mcp_python")
    path.write_bytes(encoded(value))
    manifest[module.LOCK]["sha256"]=digest(path.read_bytes())
    assert module.prepare_native_python_inputs(root,manifest)=={}
    assert module.verify_native_python_inputs(root,manifest) is None
    package(root,manifest)
    assert read_bundle(root)["origin"]["kind"]=="source-build"


def test_mcp_inert_archive_materialization_and_reverification_use_distinct_namespace(tmp_path,monkeypatch):
    from chatcopilot.core.python_materialize import materialize_python_isolated,verify_python_isolated,IsolatedPythonError
    from tests.unit.test_python_materialize import native_path,private_directory
    lock,metadata,images=v2_fixture()
    monkeypatch.setattr(subprocess,"Popen",lambda *_a,**_k:pytest.fail("Archive program executed"))
    workspace=private_directory(tmp_path/"new-private-data-lab")
    options={"expected_inputs_sha256":digest(lock),"lane":"windows_mcp_python"}
    result=materialize_python_isolated(lock,metadata,images["windows_mcp_python"],workspace=workspace,**options)
    assert result.relative_image_root.startswith("runtime/python/windows-mcp/")
    assert verify_python_isolated(lock,metadata,images["windows_mcp_python"],stage_root=Path(result.stage_root),**options)==result
    dll=Path(native_path(Path(result.stage_root)/result.relative_image_root/"python314.dll"))
    dll.write_bytes(b"tampered own inert fixture")
    with pytest.raises(IsolatedPythonError):
        verify_python_isolated(lock,metadata,images["windows_mcp_python"],stage_root=Path(result.stage_root),**options)
