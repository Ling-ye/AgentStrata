"""Pinned source/wheel layouts and private data copies never grant execution authority."""
from dataclasses import asdict, replace
import csv
import gzip
import io
import json
import os
from pathlib import Path
import subprocess

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.mcp_project import MCPProjectLimits, RECIPE, windows_mcp_project_plan
from chatcopilot.core import mcp_materialize as writer
from chatcopilot.core.python_environment import canonical_digest
from chatcopilot.core.wheel_installation import windows_mcp_wheel_installation_plan, windows_wheel_installation_plan
from tests.unit.test_python_environment import digest, private_directory
from tests.unit.test_tar_inventory import archive, member, pax_record
from tests.unit.test_wheel_installation import package

REV = "a"*40
PREFIX = "Windows-MCP-"+REV+"/"
PROJECT = b'''[project]
name="windows-mcp"
version="0.8.8"
requires-python=">=3.14"
dependencies=["demo-pkg==1.0"]
readme="README.md"
license={file="LICENSE.md"}
[project.scripts]
windows-mcp="windows_mcp.__main__:main"
[build-system]
requires=["setuptools>=61"]
build-backend="setuptools.build_meta"
[tool.setuptools.packages.find]
where=["src"]
'''


def original(*, project=PROJECT, source_extra=None, wheel_extra=None, pin=None):
    pin = pin or package(wheel_extra)
    lock = ('version=1\nrequires-python=">=3.14"\n[[package]]\nname="windows-mcp"\nversion="0.8.8"\n'
        'source={editable="."}\n[package.metadata]\nrequires-dist=[{name="demo-pkg",specifier="==1.0"}]\n'
        '[[package]]\nname="demo-pkg"\nversion="1.0"\nsource={registry="https://pypi.org/simple"}\n'
        'wheels=[{url="https://files.pythonhosted.org/demo_pkg-1.0-py3-none-any.whl",hash="sha256:'+pin.sha256+'",size='+str(len(pin.data))+'}]\n').encode()
    files = {'pyproject.toml':project,'uv.lock':lock,'README.md':b'Inert original readme\n','LICENSE.md':b'Inert original license\n',
        'src/windows_mcp/__init__.py':b'', 'src/windows_mcp/__main__.py':b'raise RuntimeError("Do not execute source")\n',
        'tests/inert.py':b'raise RuntimeError("Do not execute tests")\n'}
    files.update(source_extra or {})
    files = {name:raw for name,raw in files.items() if raw is not None}
    raw = gzip.compress(archive(member('pax_global_header',pax_record(b'comment',REV.encode()),kind=b'g'),
        *(member(PREFIX+name,data) for name,data in sorted(files.items()))),mtime=0)
    options = {'expected_source_sha256':digest(raw),'expected_revision':REV,'expected_project_sha256':digest(project),
        'expected_lock_sha256':digest(lock),'python_version':'3.14.7','recipe':RECIPE}
    return raw,(pin,),options


def plan(value):
    raw,wheels,options=value
    return windows_mcp_project_plan(raw,wheels,**options)


@pytest.fixture(autouse=True)
def no_child(monkeypatch):
    monkeypatch.setattr(subprocess,'Popen',lambda *_a,**_k:pytest.fail('MCP data mapper/copy executed a program'))


def test_source_copy_and_original_wheels_have_complete_records_and_guarded_entry():
    value=original(wheel_extra={'startup.pth':b'import never_run\n','demo_pkg-1.0.data/scripts/inert':b'raise RuntimeError()\n'})
    result=plan(value)
    files={file.path:file for file in result.files}
    assert len(result.source_files)==2
    assert files['Lib/site-packages/windows_mcp/__main__.py'].input_sha256==digest(value[0])
    assert 'Lib/site-packages/windows_mcp-0.8.8.dist-info/WHEEL' not in files
    assert files['Lib/site-packages/windows_mcp-0.8.8.dist-info/INSTALLER'].generated==b'agentstrata-source-copy\n'
    rows=list(csv.reader(io.StringIO(files['Lib/site-packages/windows_mcp-0.8.8.dist-info/RECORD'].generated.decode())))
    assert ['windows_mcp-0.8.8.dist-info/RECORD','',''] in rows
    assert any(row[0]=='../../Scripts/windows-mcp-script.py' and row[1].startswith('sha256=') for row in rows)
    assert len(result.wheels.deferred_files)==2
    assert result.entry_point.arguments==('-I','-S','Scripts/windows-mcp-script.py')
    assert result.entry_point.requires_runtime_acceptance
    wrapper=files[result.entry_point.script_path].generated
    assert b'no_site' in wrapper and b'sys.prefix' in wrapper
    assert not any((result.build_backend_executed,result.runtime_prefix_verified,result.native_abi_verified,result.installation_accepted))
    assert result==plan(value)


@pytest.mark.parametrize('field',['expected_source_sha256','expected_project_sha256','expected_lock_sha256','expected_revision'])
def test_original_pins_are_checked_before_creating_any_lab_path(tmp_path,field):
    raw,wheels,options=original()
    options[field]='0'*(40 if field=='expected_revision' else 64)
    target=tmp_path/'not-created'
    with pytest.raises(InstallError):
        writer.materialize_mcp_layout_isolated(raw,wheels,workspace=target,**options)
    assert not target.exists()


@pytest.mark.parametrize('version',['3.12.14','3.13.15','3.14.7t','3.15.1'])
def test_mcp_source_recipe_does_not_widen_other_executor_policies(version):
    raw,wheels,options=original()
    options['python_version']=version
    with pytest.raises(InstallError):
        windows_mcp_project_plan(raw,wheels,**options)


@pytest.mark.parametrize('extra',[{'setup.py':b'raise RuntimeError()'}, {'setup.cfg':b'[inert]'},
    {'src/foreign/__init__.py':b''},{'src/windows_mcp/inject.pth':b'import arbitrary'},
    {'src/windows_mcp/__main__.py':None}])
def test_unsupported_source_hooks_packages_and_startup_members_are_rejected(extra):
    with pytest.raises(InstallError):
        plan(original(source_extra=extra))


@pytest.mark.parametrize('old,new',[(b'setuptools>=61',b'setuptools>=62'),(b'where=["src"]',b'where=["other"]'),
    (b'windows_mcp.__main__:main',b'windows_mcp.__main__:other'),
    (b'version="0.8.8"',b'version="0.8.8"\ndynamic=["version"]')])
def test_source_metadata_requires_the_explicit_reviewed_copy_recipe(old,new):
    with pytest.raises(InstallError):
        plan(original(project=PROJECT.replace(old,new)))


@pytest.mark.parametrize('path',['windows_mcp/injected.py','windows_mcp.py','windows_mcp.pyd',
    'demo_pkg-1.0.dist-info/entry_points.txt'])
def test_dependency_wheels_cannot_inject_project_modules_or_claim_its_command(path):
    data=b'[console_scripts]\nwindows-mcp=demo_pkg:main\n' if path.endswith('entry_points.txt') else b'inert'
    with pytest.raises(InstallError,match='collid'):
        plan(original(wheel_extra={path:data}))


@pytest.mark.parametrize('field',['max_source_files','max_files','max_directories','max_total_bytes'])
def test_copy_plan_has_finite_aggregate_budgets(field):
    raw,wheels,options=original()
    with pytest.raises(InstallError):
        windows_mcp_project_plan(raw,wheels,limits=replace(MCPProjectLimits(),**{field:1}),**options)


def test_mcp_mapper_accepts_only_its_explicit_legacy_boolean_scope():
    pin=package({'demo_pkg-1.0.dist-info/WHEEL':b'Wheel-Version: 1.0\nRoot-Is-Purelib: True\nTag: py3-none-any\n\n'})
    result=windows_mcp_wheel_installation_plan((pin,),python_version='3.14.7',requirements=('demo-pkg',))
    assert result.recipe=='windows-mcp-wheel-layout/v1' and result.wheels[0].root_is_purelib
    with pytest.raises(InstallError):
        windows_wheel_installation_plan((pin,),python_version='3.13.15',requirements=('demo-pkg',))


def test_private_copy_and_reverification_use_originals_and_ignore_saved_claims(tmp_path):
    raw,wheels,options=original()
    lab=tmp_path/'lab'
    private_directory(lab) if os.name=='nt' else lab.mkdir(mode=0o700)
    observed=writer.materialize_mcp_layout_isolated(raw,wheels,workspace=lab,**options)
    assert observed==writer.verify_mcp_layout_isolated(raw,wheels,stage_root=Path(observed.stage_root),**options)
    assert observed.plan_sha256==canonical_digest(asdict(windows_mcp_project_plan(raw,wheels,**options)))
    assert observed.state=='staged-data-only' and not observed.executable_prefix_created
    (tmp_path/'untrusted-claim.json').write_text(json.dumps({'installation_accepted':True,'files':0}))
    copied=Path(observed.stage_root)/'Lib/site-packages/windows_mcp/__main__.py'
    copied.write_bytes(b'inert corruption')
    with pytest.raises(writer.IsolatedMCPError):
        writer.verify_mcp_layout_isolated(raw,wheels,stage_root=Path(observed.stage_root),**options)
    assert copied.read_bytes()==b'inert corruption'


def test_copy_failure_retains_partial_new_stage_and_does_not_modify_old_data(tmp_path,monkeypatch):
    raw,wheels,options=original()
    lab=tmp_path/'lab'
    private_directory(lab) if os.name=='nt' else lab.mkdir(mode=0o700)
    old=tmp_path/'old-data'
    old.write_bytes(b'preserve')
    def fail(*_args,**_kwargs):
        raise OSError('injected bounded-copy failure')
    monkeypatch.setattr(writer,'_write',fail)
    with pytest.raises(writer.IsolatedMCPError) as caught:
        writer.materialize_mcp_layout_isolated(raw,wheels,workspace=lab,**options)
    assert Path(caught.value.stage_root).is_dir()
    assert old.read_bytes()==b'preserve'

def test_insufficient_space_is_rejected_before_private_stage_creation(tmp_path,monkeypatch):
    from types import SimpleNamespace
    raw,wheels,options=original()
    lab=tmp_path/'lab'
    private_directory(lab) if os.name=='nt' else lab.mkdir(mode=0o700)
    monkeypatch.setattr(writer.shutil,'disk_usage',lambda _path:SimpleNamespace(free=1))
    monkeypatch.setattr(writer,'IsolatedTree',lambda *_a,**_k:pytest.fail('Storage opened before space preflight'))
    with pytest.raises(InstallError,match='free space'):
        writer.materialize_mcp_layout_isolated(raw,wheels,workspace=lab,**options)
    assert not list(lab.iterdir())


def test_read_only_reverification_does_not_require_free_space(tmp_path,monkeypatch):
    from types import SimpleNamespace
    raw,wheels,options=original()
    lab=tmp_path/'lab'
    private_directory(lab) if os.name=='nt' else lab.mkdir(mode=0o700)
    observed=writer.materialize_mcp_layout_isolated(raw,wheels,workspace=lab,**options)
    monkeypatch.setattr(writer.shutil,'disk_usage',lambda _path:SimpleNamespace(free=0))
    assert writer.verify_mcp_layout_isolated(raw,wheels,stage_root=Path(observed.stage_root),**options)==observed
