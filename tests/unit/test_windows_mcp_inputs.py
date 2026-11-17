"""Synthetic archives prove input/refusal behavior, never real MCP execution or independent acceptance."""
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from chatcopilot.core.dependency_closure import windows_dependency_input_inventory,windows_mcp_dependency_input_inventory
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.deployment import windows_mcp_inputs as module
from chatcopilot.deployment.application_bundle import read_bundle
from chatcopilot.deployment.application_runtime import install_bundle
from chatcopilot.deployment.runtime_requirements import MEMBER as REQUIREMENTS,prepare_runtime_requirements
from chatcopilot.deployment.windows_python_inputs import prepare_native_python_inputs
from tests.unit.test_dependency_closure import wheel
from tests.unit.test_python_plan import encoded,v2_fixture
from tests.unit.test_tar_inventory import archive,member,pax_record
from tests.unit.test_windows_python_inputs import payload as _payload

ROOT=Path(__file__).resolve().parents[2]
POPEN=subprocess.Popen
REV='a'*40


def h(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def inputs(tmp_path,monkeypatch):
    payload,manifest,metadata=_payload.__wrapped__(tmp_path,monkeypatch)
    original=wheel('demo',python='>=3.14')
    project=b'[project]\nname="windows-mcp"\nversion="0.8.8"\nrequires-python=">=3.14"\ndependencies=["demo==1.0"]\n'
    lock=('version=1\nrevision=1\nrequires-python=">=3.14"\n[[package]]\nname="windows-mcp"\nversion="0.8.8"\nsource={editable="."}\n'
        '[package.metadata]\nrequires-dist=[{name="demo",specifier="==1.0"}]\n[[package]]\nname="demo"\nversion="1.0"\n'
        'source={registry="https://pypi.org/simple"}\nwheels=[{url="https://files.pythonhosted.org/demo-1.0-py3-none-any.whl",hash="sha256:'+original.sha256+'",size='+str(len(original.data))+'}]\n').encode()
    prefix='Windows-MCP-'+REV+'/'
    source=gzip.compress(archive(member('pax_global_header',pax_record(b'comment',REV.encode()),kind=b'g'),
        member(prefix+'pyproject.toml',project),member(prefix+'uv.lock',lock),member(prefix+'src/inert.py',b'raise RuntimeError("inert")\n')),mtime=0)
    policy={'schema':module.SCHEMA,'repository':'https://github.com/CursorTouch/Windows-MCP','revision':REV,
        'archive_url':'https://codeload.github.com/CursorTouch/Windows-MCP/tar.gz/'+REV,'archive_sha256':h(source),
        'package_version':'0.8.8','python':'3.14.7','source_metadata':{'pyproject.toml':h(project),'uv.lock':h(lock)},
        'uv':{'version':'0.12.5','url':'https://github.com/astral-sh/uv/releases/download/0.12.5/uv-x86_64-pc-windows-msvc.zip','sha256':'b'*64,'binary_sha256':'c'*64}}
    (payload/'source'/module.LOCK).write_bytes(encoded(policy))
    manifest[module.LOCK]['sha256']=h(encoded(policy))
    source_path=tmp_path/'original-source.tar.gz'
    source_path.write_bytes(source)
    py_path=tmp_path/'original-python.tar.gz'
    py_path.write_bytes(v2_fixture()[2]['windows_mcp_python'])
    cache=tmp_path/'original-wheels'
    cache.mkdir()
    (cache/'demo-1.0-py3-none-any.whl').write_bytes(original.data)
    prepare_native_python_inputs(payload,manifest,metadata=metadata)
    return payload,manifest,source_path,py_path,cache


def prepare(values,*,wheels=True):
    payload,manifest,source,python,cache=values
    return module.prepare_native_mcp_inputs(payload,manifest,'bots/demo/bot.yaml',source_archive=source,python_archive=python,
        wheel_cache=cache if wheels else None)


def package(payload,manifest):
    from chatcopilot.core.source_snapshot import manifest_digest
    files={path.relative_to(payload).as_posix():h(path.read_bytes()) for path in payload.rglob('*')
        if path.is_file() and path.relative_to(payload).parts[0]!='source' and path.name not in {'application.json','artifacts.json'}}
    catalog=encoded({'source_digest':manifest_digest(manifest),'files':files})
    (payload/'artifacts.json').write_bytes(catalog)
    (payload/'application.json').write_bytes(encoded({'schema':'agentstrata-windows-runtime/v1','platform':'windows-x86_64',
        'bot_id':'demo','bot_relative':'bots/demo/bot.yaml','source_digest':manifest_digest(manifest),'source_manifest':manifest,
        'origin':{'kind':'source-build','base_revision':'a'*40},'artifacts_digest':h(catalog)}))


def test_production_source_archives_and_wheel_metadata_bind_complete_scope_but_never_runtime(inputs):
    payload,manifest,*_=inputs
    rows=prepare(inputs)
    report=module.verify_native_mcp_inputs(payload,manifest,'bots/demo/bot.yaml',rows)
    assert report['state']=='verified-wheel-inputs' and report['wheel_archives_verified']
    assert len(report['dependency_inputs']['nodes'])==1 and report['wheel_verification']['archives']==1
    assert report['wheel_verification']['closure']['scope']=='pinned-windows-mcp-distributions'
    assert not report['prefix_loading_abi_verified'] and not report['project_installation_verified'] and not report['installation_accepted']
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    requirements=json.loads((payload/REQUIREMENTS).read_bytes())
    scope=next(row for row in requirements['scopes'] if row['id']=='mcp:windows-mcp')
    assert requirements['state']=='incomplete' and all(scope['inputs'][name]==pin for name,pin in rows.items())
    assert 'mcp-wheel-installation-layout-unverified' in scope['blockers'] and 'mcp-python-archive-not-audited' not in scope['blockers']
    package(payload,manifest)
    assert read_bundle(payload)['bot_id']=='demo'


def test_archive_audit_without_wheel_bytes_does_not_claim_metadata_closure(inputs):
    payload,manifest,*_=inputs
    rows=prepare(inputs,wheels=False)
    report=module.verify_native_mcp_inputs(payload,manifest,'bots/demo/bot.yaml',rows)
    assert report['state']=='audited-archives' and not report['wheel_archives_verified']
    assert report['dependency_inputs']['scope']=='upstream-locked-declarations'


@pytest.mark.parametrize('removed',[module.SOURCE,module.PYTHON,module.MEMBER,module.WHEELS+'/demo-1.0-py3-none-any.whl'])
def test_install_entry_rejects_removed_inputs_even_after_catalog_rehash_before_destination_write(inputs,tmp_path,removed):
    payload,manifest,*_=inputs
    prepare(inputs)
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    (payload/removed).unlink()
    package(payload,manifest)
    target=tmp_path/'not-installed'
    with pytest.raises((ValueError,InstallError)):
        install_bundle(payload,target)
    assert not target.exists()


@pytest.mark.parametrize('change',['roots','nodes','accepted','state','closure','python-lane'])
def test_rehashing_report_cannot_replace_original_sources_closure_or_installation_acceptance(inputs,change):
    payload,manifest,*_=inputs
    prepare(inputs)
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    value=json.loads((payload/module.MEMBER).read_bytes())
    if change=='roots':
        value['dependency_inputs']['roots']=[]
    elif change=='nodes':
        value['dependency_inputs']['nodes']=[]
    elif change=='accepted':
        value['installation_accepted']=True
    elif change=='state':
        value['state']='accepted'
    elif change=='closure':
        value['wheel_verification']['closure']['nodes']=[]
    elif change=='python-lane':
        value['python_archive']['lane']='host_python'
    (payload/module.MEMBER).write_bytes(encoded(value))
    package(payload,manifest)
    with pytest.raises(ValueError,match='report differs'):
        read_bundle(payload)


@pytest.mark.parametrize('field',['revision','metadata','python'])
def test_new_source_digest_does_not_fix_a_substituted_archive_subject(inputs,field):
    payload,manifest,*_=inputs
    value=json.loads((payload/'source'/module.LOCK).read_bytes())
    if field=='revision':
        value['revision']='b'*40
        value['archive_url']='https://codeload.github.com/CursorTouch/Windows-MCP/tar.gz/'+value['revision']
    elif field=='metadata':
        value['source_metadata']['uv.lock']='0'*64
    else:
        value['python']='3.13.15'
    raw=encoded(value)
    (payload/'source'/module.LOCK).write_bytes(raw)
    manifest[module.LOCK]['sha256']=h(raw)
    with pytest.raises((ValueError,InstallError)):
        prepare(inputs)
    assert not (payload/'mcp-inputs').exists()


def test_extra_cached_startup_code_and_corrupt_original_wheels_are_never_repaired_or_written(inputs):
    payload,_manifest,_source,_python,cache=inputs
    (cache/'injected.pth').write_bytes(b'import unexpected')
    with pytest.raises(ValueError,match='missing or extra'):
        prepare(inputs)
    assert (cache/'injected.pth').exists() and not (payload/'mcp-inputs').exists()


def test_mcp_metadata_scope_does_not_widen_host_or_other_python_versions():
    original=wheel('app',python='>=3.14',requires=("leaf; python_version == '3.14'",))
    leaf=wheel('leaf')
    result=windows_mcp_dependency_input_inventory((original,leaf),python_version='3.14.7',requirements=('app',))
    assert len(result.closure.nodes)==2 and not result.closure.native_abi_verified
    with pytest.raises(InstallError):
        windows_dependency_input_inventory((original,leaf),python_version='3.14.7',requirements=('app',))
    with pytest.raises(InstallError):
        windows_mcp_dependency_input_inventory((original,),python_version='3.13.15',requirements=('app',))
    with pytest.raises(InstallError):
        windows_mcp_dependency_input_inventory((original,),python_version='3.14.7',requirements=('app',))
    with pytest.raises(InstallError):
        windows_mcp_dependency_input_inventory((original,leaf,wheel('unrelated')),python_version='3.14.7',requirements=('app',))


@pytest.mark.parametrize('value',['True','False','TRUE','false-extra'])
def test_legacy_root_boolean_scope_never_changes_original_or_installation_policy(value):
    from tests.unit.test_python_environment import wheel as bytes_wheel
    from chatcopilot.core.wheel_plan import PinnedWheel,audit_windows_wheel_inputs,audit_windows_mcp_wheel_inputs,windows_wheel_plan
    data=bytes_wheel({'demo_pkg-1.0.dist-info/WHEEL':('Wheel-Version: 1.0\nRoot-Is-Purelib: '+value+'\nTag: py3-none-any\n\n').encode()})
    pin=PinnedWheel(data,h(data),'demo-pkg','1.0')
    options={'compatible_tags':('py3-none-any',)}
    with pytest.raises(InstallError):
        audit_windows_wheel_inputs((pin,),**options)
    with pytest.raises(InstallError):
        windows_wheel_plan((pin,),**options)
    if value in {'True','False'}:
        result=audit_windows_mcp_wheel_inputs((pin,),**options)
        assert result.schema=='agentstrata-windows-mcp-wheel-input-inventory/v1'
        assert result.wheels[0].archive_sha256==pin.sha256
    else:
        with pytest.raises(InstallError):
            audit_windows_mcp_wheel_inputs((pin,),**options)


def test_legacy_source_keeps_its_incomplete_contract_and_cannot_adopt_new_artifacts(inputs):
    payload,manifest,*_=inputs
    value=json.loads((payload/'source'/module.LOCK).read_bytes())
    value.pop('schema')
    value.pop('source_metadata')
    (payload/'source'/module.LOCK).write_bytes(encoded(value))
    manifest[module.LOCK]['sha256']=h(encoded(value))
    assert prepare(inputs)=={}
    assert module.verify_native_mcp_inputs(payload,manifest,'bots/demo/bot.yaml') is None
    (payload/module.MEMBER).write_bytes(b'{}')
    with pytest.raises(ValueError,match='frozen archive policy'):
        module.verify_native_mcp_inputs(payload,manifest,'bots/demo/bot.yaml')


def test_ambiguous_or_missing_locked_nodes_never_fall_back_to_installed_packages():
    project={'project':{'name':'windows-mcp','version':'0.8.8','requires-python':'>=3.14','dependencies':['demo==1']}}
    local={'name':'windows-mcp','version':'0.8.8','source':{'editable':'.'},'metadata':{'requires-dist':[{'name':'demo','specifier':'==1'}]}}
    lock={'version':1,'requires-python':'>=3.14','package':[local]}
    with pytest.raises(ValueError,match='exactly one'):
        module.locked_dependency_inputs(project,lock,'3.14.7')
    node={'name':'demo','version':'1','source':{'registry':'https://pypi.org/simple'},'wheels':[]}
    lock['package']=[local,node,deepcopy(node)]
    with pytest.raises(ValueError,match='exactly one'):
        module.locked_dependency_inputs(project,lock,'3.14.7')


@pytest.mark.skipif(os.name!='nt',reason='Native Windows PowerShell input inspection only')
def test_bootstrap_input_inspection_does_not_create_installation_or_bootstrap_data(inputs,tmp_path,monkeypatch):
    payload,manifest,*_=inputs
    prepare(inputs)
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    package(payload,manifest)
    install=tmp_path/'must-not-install'
    appdata=tmp_path/'must-not-bootstrap'
    executable=str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
    script=ROOT/'integrations/live2d_desktop/scripts/bootstrap-windows.ps1'
    with monkeypatch.context() as context:
        context.setattr(subprocess,'Popen',POPEN)
        result=subprocess.run([executable,'-NoProfile','-NonInteractive','-File',str(script),'-BundleRoot',str(payload),
            '-InstallRoot',str(install),'-InspectInputsOnly'],capture_output=True,timeout=20,env={
                'SystemRoot':os.environ['SystemRoot'],'LOCALAPPDATA':str(appdata),'USERPROFILE':str(tmp_path)})
    assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
    value=json.loads(result.stdout.decode('utf-8-sig'))
    assert value['mode']=='inspection-only' and not value['runtime_execution_allowed'] and not value['runtime_acceptance_verified']
    assert not install.exists() and not appdata.exists()
