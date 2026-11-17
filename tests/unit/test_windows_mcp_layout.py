"""Production layout artifacts are rederived from pinned sources and original wheel bytes."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from chatcopilot.deployment import windows_mcp_inputs as module
from chatcopilot.deployment.application_bundle import read_bundle
from chatcopilot.deployment.application_runtime import install_bundle
from chatcopilot.deployment.runtime_requirements import MEMBER as REQUIREMENTS,prepare_runtime_requirements
from chatcopilot.deployment.windows_python_inputs import prepare_native_python_inputs
from tests.unit.test_mcp_project import original
from tests.unit.test_python_plan import digest,encoded,v2_fixture
from tests.unit.test_windows_mcp_inputs import package
from tests.unit.test_windows_python_inputs import payload as base_payload

ROOT=Path(__file__).resolve().parents[2]
POPEN=subprocess.Popen


@pytest.fixture
def inputs(tmp_path,monkeypatch):
    payload,manifest,metadata=base_payload.__wrapped__(tmp_path,monkeypatch)
    source,wheels,pins=original()
    policy={'schema':module.MAPPED_SCHEMA,'installation_recipe':pins['recipe'],
        'repository':'https://github.com/CursorTouch/Windows-MCP','revision':pins['expected_revision'],
        'archive_url':'https://codeload.github.com/CursorTouch/Windows-MCP/tar.gz/'+pins['expected_revision'],
        'archive_sha256':pins['expected_source_sha256'],'package_version':'0.8.8','python':'3.14.7',
        'source_metadata':{'pyproject.toml':pins['expected_project_sha256'],'uv.lock':pins['expected_lock_sha256']},
        'uv':{'version':'0.12.5','url':'https://github.com/astral-sh/uv/releases/download/0.12.5/uv-x86_64-pc-windows-msvc.zip',
            'sha256':'b'*64,'binary_sha256':'c'*64}}
    raw=encoded(policy)
    (payload/'source'/module.LOCK).write_bytes(raw)
    manifest[module.LOCK]['sha256']=digest(raw)
    original_path=tmp_path/'source.tar.gz'
    original_path.write_bytes(source)
    python_path=tmp_path/'python.tar.gz'
    python_path.write_bytes(v2_fixture()[2]['windows_mcp_python'])
    cache=tmp_path/'wheels'
    cache.mkdir()
    (cache/'demo_pkg-1.0-py3-none-any.whl').write_bytes(wheels[0].data)
    prepare_native_python_inputs(payload,manifest,metadata=metadata)
    return payload,manifest,original_path,python_path,cache


def prepare(values):
    payload,manifest,source,python,cache=values
    return module.prepare_native_mcp_inputs(payload,manifest,'bots/demo/bot.yaml',source_archive=source,python_archive=python,wheel_cache=cache)


def test_production_prepare_read_scope_and_artifact_binding_include_merged_layout(inputs):
    payload,manifest,*_=inputs
    rows=prepare(inputs)
    report=module.verify_native_mcp_inputs(payload,manifest,'bots/demo/bot.yaml',rows)
    layout=report['installation_layout_plan']
    assert layout['state']=='planned' and layout['recipe']=='windows-mcp-source-copy/v1'
    assert module.LAYOUT in rows and not layout['installation_accepted']
    assert not layout['build_backend_executed']
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    requirements=json.loads((payload/REQUIREMENTS).read_bytes())
    scope=next(row for row in requirements['scopes'] if row['id']=='mcp:windows-mcp')
    assert requirements['state']=='incomplete' and scope['inputs'][module.LAYOUT]==rows[module.LAYOUT]
    assert 'mcp-source-and-wheel-layout-not-installed-or-accepted' in scope['blockers']
    package(payload,manifest)
    assert read_bundle(payload)['bot_id']=='demo'


@pytest.mark.parametrize('change',['missing','source-files','accepted','entry','mapping-hash'])
def test_layout_rehash_cannot_remove_sources_change_entry_or_accept_installation(inputs,tmp_path,change):
    payload,manifest,*_=inputs
    prepare(inputs)
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    path=payload/module.LAYOUT
    value=json.loads(path.read_bytes())
    if change=='missing':
        path.unlink()
    elif change=='source-files':
        value['source_files']=[]
    elif change=='accepted':
        value['installation_accepted']=True
    elif change=='entry':
        value['entry_point']['module']='foreign'
    else:
        value['mapping_plan_sha256']='0'*64
    if change!='missing':
        path.write_bytes(encoded(value))
    package(payload,manifest)
    destination=tmp_path/'must-not-install'
    with pytest.raises(ValueError):
        install_bundle(payload,destination)
    assert not destination.exists()


def test_mapped_source_policy_refuses_an_absent_complete_original_wheel_set(inputs,tmp_path):
    payload,manifest,source,python,_cache=inputs
    with pytest.raises(ValueError,match='complete original wheel set'):
        module.prepare_native_mcp_inputs(payload,manifest,'bots/demo/bot.yaml',source_archive=source,python_archive=python)
    assert not (payload/'mcp-inputs').exists()


@pytest.mark.skipif(os.name!='nt',reason='Native Windows PowerShell mapped-input inspection only')
def test_native_bootstrap_inspects_v3_layout_without_creating_or_running_a_prefix(inputs,tmp_path,monkeypatch):
    payload,manifest,*_=inputs
    prepare(inputs)
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    package(payload,manifest)
    target=tmp_path/'must-not-install'
    appdata=tmp_path/'must-not-bootstrap'
    executable=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    script=ROOT/'integrations/live2d_desktop/scripts/bootstrap-windows.ps1'
    with monkeypatch.context() as context:
        context.setattr(subprocess,'Popen',POPEN)
        result=subprocess.run([str(executable),'-NoProfile','-NonInteractive','-File',str(script),'-BundleRoot',str(payload),
            '-InstallRoot',str(target),'-InspectInputsOnly'],capture_output=True,timeout=20,
            env={'SystemRoot':os.environ['SystemRoot'],'LOCALAPPDATA':str(appdata),'USERPROFILE':str(tmp_path)})
    assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
    state=json.loads(result.stdout.decode('utf-8-sig'))
    assert state['mode']=='inspection-only' and not state['runtime_execution_allowed'] and not state['runtime_acceptance_verified']
    assert not target.exists() and not appdata.exists()
