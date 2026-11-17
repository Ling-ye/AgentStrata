"""Production input readers rederive projects; catalog hashes cannot grant runtime acceptance."""
import json
from pathlib import Path
import subprocess

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.project_patch import RECIPE
from chatcopilot.deployment import windows_plugin_inputs as module
from chatcopilot.deployment import windows_python_inputs as python_inputs
from chatcopilot.deployment.application_bundle import read_bundle
from chatcopilot.deployment.application_runtime import install_bundle
from chatcopilot.deployment.runtime_requirements import MEMBER as REQUIREMENTS, prepare_runtime_requirements
from tests.unit.test_project_patch import private_directory
from tests.unit.test_python_plan import encoded,digest
from tests.unit.test_windows_plugin_inputs import inputs as inputs, frozen, policy
from tests.unit.test_windows_mcp_inputs import package
from tests.unit.test_project_patch import original,REV


def refresh_python(payload,manifest):
    for name in (python_inputs.MEMBER,python_inputs.METADATA):
        (payload/name).unlink()
    python_inputs.prepare_native_python_inputs(payload,manifest,metadata=payload.parent/'original-supplier.json')


@pytest.fixture
def projects(inputs,tmp_path,monkeypatch):
    monkeypatch.setattr(subprocess,'Popen',lambda *_a,**_k:pytest.fail('project input/staging executed upstream software'))
    payload,manifest,wheels=inputs
    value=policy()
    value.update(schema=module.PROJECT_SCHEMA,recipe='windows-vtuber-original-inputs/v3',wheel_archive_policy=module.ARCHIVE_POLICY,
        projects={name:{'recipe':RECIPE,'allowed_cjk_paths':[]} for name in ('backend','frontend')})
    frozen(payload,manifest,module.LOCK,encoded(value))
    upstream,artifacts={},{}
    cache=tmp_path/'original-projects'
    cache.mkdir()
    for name in ('backend','frontend'):
        raw,patch,_pins=original(name)
        repository='Open-LLM-VTuber' if name=='backend' else 'Open-LLM-VTuber-Web'
        upstream[name]={'repository':'https://github.com/Open-LLM-VTuber/'+repository+'.git','revision':REV,'patch':'patches/'+name+'.patch'}
        artifacts[name]={'url':'https://codeload.github.com/Open-LLM-VTuber/'+repository+'/tar.gz/'+REV,'sha256':digest(raw)}
        (cache/(name+'.tar.gz')).write_bytes(raw)
        frozen(payload,manifest,module._RESOURCE+'patches/'+name+'.patch',patch)
    frozen(payload,manifest,module._SOURCES['upstream'],encoded(upstream))
    frozen(payload,manifest,module._SOURCES['artifacts'],encoded(artifacts))
    refresh_python(payload,manifest)
    return payload,manifest,wheels,cache


def test_actual_preparer_reader_scope_and_private_stage_consume_the_same_originals(projects,tmp_path):
    payload,manifest,wheels,cache=projects
    rows=module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=wheels,project_cache=cache)
    report=module.verify_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',rows)
    assert report['schema'].endswith('/v3') and report['project_sources_verified'] and report['wheel_closure_verified']
    assert len(report['project_source_plans'])==2 and len(report['project_archives'])==2
    assert not report['project_installation_verified'] and not report['installation_accepted'] and not report['assets_verified']
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    runtime=json.loads((payload/REQUIREMENTS).read_bytes())
    scope=next(row for row in runtime['scopes'] if row['id']=='plugin-components')
    assert runtime['state']=='incomplete' and all(scope['inputs'][name]==pin for name,pin in rows.items())
    lab=private_directory(tmp_path/'lab')
    stage=module.stage_native_plugin_projects(payload,manifest,'bots/demo/bot.yaml',workspace=lab)
    assert stage['files']==6 and not stage['installation_accepted'] and not stage['target_programs_executed']
    assert (Path(stage['stage_root'])/'runtime/apps/backend/main.py').read_bytes()==b'keep\nafter\n'
    package(payload,manifest)
    assert read_bundle(payload)['bot_id']=='demo'


def test_projects_can_be_audited_independently_but_absent_wheels_still_block(projects):
    payload,manifest,_wheels,cache=projects
    module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',project_cache=cache)
    report=module.verify_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml')
    assert report['project_sources_verified'] and not report['wheel_closure_verified']
    assert 'plugin-complete-original-wheel-set-absent' in report['blockers']


def test_source_only_v3_declarations_keep_project_and_asset_blockers(projects):
    payload,manifest,_wheels,_cache=projects
    module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml')
    report=module.verify_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml')
    assert not report['project_sources_verified']
    assert 'plugin-project-archives-and-patches-unverified' in report['blockers']


@pytest.mark.parametrize('change',['archive','extra','missing','patch','url','revision','policy'])
def test_untrusted_or_incomplete_project_inputs_fail_before_outputs(projects,change):
    payload,manifest,_wheels,cache=projects
    if change=='archive':
        path=cache/'backend.tar.gz'
        path.write_bytes(path.read_bytes()+b'changed')
    elif change=='extra':
        (cache/'local-receipt.json').write_text('{}')
    elif change=='missing':
        (cache/'frontend.tar.gz').unlink()
    elif change=='patch':
        name=module._RESOURCE+'patches/backend.patch'
        frozen(payload,manifest,name,(payload/'source'/name).read_bytes().replace(b'-before',b'-wrong'))
    elif change=='url':
        name=module._SOURCES['artifacts']
        doc=json.loads((payload/'source'/name).read_bytes())
        doc['backend']['url']='https://example.invalid/untrusted.tar.gz'
        frozen(payload,manifest,name,encoded(doc))
    elif change=='revision':
        name=module._SOURCES['upstream']
        doc=json.loads((payload/'source'/name).read_bytes())
        doc['backend']['revision']='b'*40
        frozen(payload,manifest,name,encoded(doc))
    else:
        doc=json.loads((payload/'source'/module.LOCK).read_bytes())
        doc['projects']['backend']['installed_state']={}
        frozen(payload,manifest,module.LOCK,encoded(doc))
    refresh_python(payload,manifest)
    with pytest.raises((ValueError,InstallError)):
        module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',project_cache=cache)
    assert not (payload/module.MEMBER).exists() and not (payload/module.PROJECTS).exists()


@pytest.mark.parametrize('change',['plan-file','plan-pin','accepted','remove','raw'])
def test_rehashing_catalog_cannot_forge_project_mapping_or_omit_originals(projects,tmp_path,change):
    payload,manifest,_wheels,cache=projects
    module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',project_cache=cache)
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    report=json.loads((payload/module.MEMBER).read_bytes())
    if change=='plan-file':
        report['project_source_plans'][0]['files'][0]['sha256']='0'*64
    elif change=='plan-pin':
        report['project_source_plans'][0]['archive_sha256']='0'*64
    elif change=='accepted':
        report['installation_accepted']=True
    elif change=='remove':
        (payload/module.PROJECTS/'backend.tar.gz').unlink()
    else:
        path=payload/module.PROJECTS/'backend.tar.gz'
        path.write_bytes(path.read_bytes()+b'tamper')
    (payload/module.MEMBER).write_bytes(encoded(report))
    package(payload,manifest)
    destination=tmp_path/'never-install'
    with pytest.raises((ValueError,InstallError)):
        install_bundle(payload,destination)
    assert not destination.exists()


def test_legacy_source_policy_does_not_inherit_project_authority(inputs,tmp_path):
    payload,manifest,_cache=inputs
    cache=tmp_path/'projects'
    cache.mkdir()
    with pytest.raises(ValueError,match='frozen project policy'):
        module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',project_cache=cache)
    assert not (payload/module.MEMBER).exists()


def test_prepare_cli_exposes_original_projects_and_keeps_vendor_execution_gated():
    script=Path(__file__).resolve().parents[2]/'integrations/live2d_desktop/scripts/prepare-runtime.py'
    text=script.read_text()
    assert '--plugin-projecthouse' in text and '--plugin-project-lab' in text
    assert text.index('stage_native_plugin_projects(args.output')<text.index('if plugin_files:')<text.index('args.build_root.mkdir')


@pytest.mark.parametrize('tampered',[False,True])
def test_real_plugin_provisioner_revalidates_inputs_and_refuses_before_copy_or_models(projects,tmp_path,monkeypatch,tampered):
    from types import SimpleNamespace
    from chatcopilot.core.source_snapshot import manifest_digest
    payload,manifest,_wheels,cache=projects
    module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',project_cache=cache)
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    if tampered:
        (payload/module.PROJECTS/'backend.tar.gz').unlink()
    package(payload,manifest)
    plugin_source=Path(__file__).resolve().parents[2]/'integrations/open_llm_vtuber'
    monkeypatch.syspath_prepend(str(plugin_source))
    from agentstrata_vtuber import native_prepare
    record=SimpleNamespace(artifact_digest=digest((payload/'artifacts.json').read_bytes()),
        delivered_digest=manifest_digest(manifest),root=tmp_path/'host')
    monkeypatch.setattr(native_prepare,'installation',lambda _root:record)
    for name in ('configure_application_client','prepare_assets','prepare_models'):
        monkeypatch.setattr(native_prepare,name,lambda *_a,**_k:pytest.fail('incomplete runtime touched client/model data'))
    target=tmp_path/'untouched-plugin'
    with pytest.raises((ValueError,InstallError)) as error:
        native_prepare.prepare({'root':str(target),'host':{'application':{'root':str(record.root)}},'artifacts':str(payload)})
    if not tampered:
        assert 'original inputs remain data' in str(error.value)
    assert not target.exists() and not record.root.exists()
