"""Complete source locks and original bytes are required; local build claims do not admit plugin runtime."""
from copy import deepcopy
import json

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.deployment import windows_plugin_inputs as module
from chatcopilot.deployment.application_bundle import read_bundle
from chatcopilot.deployment.application_runtime import install_bundle
from chatcopilot.deployment.runtime_requirements import MEMBER as REQUIREMENTS, prepare_runtime_requirements
from chatcopilot.deployment.windows_dependencies import canonical_requirements
from chatcopilot.deployment.windows_python_inputs import prepare_native_python_inputs
from tests.unit.test_dependency_closure import wheel
from tests.unit.test_python_plan import encoded, digest
from tests.unit.test_windows_mcp_inputs import package
from tests.unit.test_windows_python_inputs import payload as base_payload
from tests.unit.test_langdetect_wheel import paired
from tests.unit.test_dependency_closure import wheel as dependency_wheel


def policy():
    return {'schema':module.SCHEMA,'plugin':'open-llm-vtuber','recipe':'windows-vtuber-original-inputs/v1',
        'python_lane':'vtuber_python',**module._SOURCES,'direct_wheels':{},'source_builds':{}}


def frozen(payload,manifest,name,raw):
    target=payload/'source'/name
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(raw)
    manifest[name]={'sha256':digest(raw),'executable':False}


@pytest.fixture
def inputs(tmp_path,monkeypatch,request):
    payload,manifest,metadata=base_payload.__wrapped__(tmp_path,monkeypatch)
    pin=wheel('demo-pkg',python='>=3.12,<3.13')
    source_policy=policy()
    source_policy.update(getattr(request,'param',{}))
    rows={module.LOCK:encoded(source_policy),module._SOURCES['requirements_input']:b'Demo_Pkg==1.0\n',
        module._SOURCES['requirements_lock']:('demo-pkg==1.0 \\\n    --hash=sha256:'+pin.sha256+'\n').encode(),
        module._SOURCES['upstream']:encoded({'backend':{},'frontend':{}}),
        module._SOURCES['artifacts']:encoded({'backend':{},'frontend':{}})}
    for name,raw in rows.items():
        frozen(payload,manifest,name,raw)
    cache=tmp_path/'original-wheels'
    cache.mkdir()
    (cache/'demo_pkg-1.0-py3-none-any.whl').write_bytes(pin.data)
    prepare_native_python_inputs(payload,manifest,metadata=metadata)
    return payload,manifest,cache


def test_complete_original_wheel_closure_binds_production_scope_and_reader_but_not_execution(inputs):
    payload,manifest,cache=inputs
    rows=module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=cache)
    value=module.verify_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',rows)
    assert value['state']=='verified-wheel-inputs' and value['wheel_closure_verified']
    assert value['dependencies']['roots']==['demo-pkg==1.0']
    assert not value['installation_accepted'] and not value['assets_verified'] and not value['project_installation_verified']
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    requirements=json.loads((payload/REQUIREMENTS).read_bytes())
    scope=next(row for row in requirements['scopes'] if row['id']=='plugin-components')
    assert requirements['state']=='incomplete' and all(scope['inputs'][name]==pin for name,pin in rows.items())
    package(payload,manifest)
    assert read_bundle(payload)['bot_id']=='demo'


def test_full_source_declarations_do_not_claim_missing_original_wheel_metadata(inputs):
    payload,manifest,_cache=inputs
    rows=module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml')
    value=module.verify_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',rows)
    assert value['state']=='locked-declarations' and not value['wheel_closure_verified']
    assert 'plugin-complete-original-wheel-set-absent' in value['blockers']


@pytest.mark.parametrize('change',['roots','hashes','accepted','metadata-closure','project'])
def test_catalog_rehash_cannot_replace_source_roots_original_hashes_or_acceptance(inputs,change):
    payload,manifest,_cache=inputs
    module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml')
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    value=json.loads((payload/module.MEMBER).read_bytes())
    if change=='roots':
        value['dependencies']['roots']=[]
    elif change=='hashes':
        value['dependencies']['locked'][0]['hashes']=['0'*64]
    elif change=='accepted':
        value['installation_accepted']=True
    elif change=='metadata-closure':
        value['wheel_closure_verified']=True
    else:
        value['project_installation_verified']=True
    (payload/module.MEMBER).write_bytes(encoded(value))
    package(payload,manifest)
    with pytest.raises(ValueError,match='report differs'):
        read_bundle(payload)


@pytest.mark.parametrize('removed',[module.MEMBER,module.WHEELS+'/demo_pkg-1.0-py3-none-any.whl'])
def test_install_entry_rejects_omitted_plugin_inputs_before_destination_write(inputs,tmp_path,removed):
    payload,manifest,cache=inputs
    module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=cache)
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    (payload/removed).unlink()
    package(payload,manifest)
    target=tmp_path/'must-not-install'
    with pytest.raises((ValueError,InstallError)):
        install_bundle(payload,target)
    assert not target.exists()


def test_derived_build_receipt_cannot_register_a_new_wheel_hash(inputs):
    payload,manifest,cache=inputs
    path=cache/'demo_pkg-1.0-py3-none-any.whl'
    path.write_bytes(path.read_bytes()+b'derived output')
    (payload/'build.json').write_bytes(encoded({'wheel_sha256':digest(path.read_bytes()),'accepted':True}))
    with pytest.raises(ValueError,match='absent from the frozen'):
        module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=cache)
    assert not (payload/module.MEMBER).exists() and not (payload/'plugin-wheels').exists()


@pytest.mark.parametrize('inputs',[{'source_builds':{'demo-pkg':{'version':'1.0','state':'unadmitted'}}}],indirect=True)
def test_source_builds_remain_blocked_even_when_an_original_wheel_set_is_verified(inputs):
    payload,manifest,cache=inputs
    module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=cache)
    report=module.verify_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml')
    assert report['wheel_closure_verified']
    assert 'plugin-source-build-not-admitted:demo-pkg' in report['blockers'] and not report['installation_accepted']


def test_missing_or_extra_original_wheels_are_rejected_before_outputs(inputs):
    payload,manifest,cache=inputs
    (cache/'injected.pth').write_bytes(b'import arbitrary')
    with pytest.raises(ValueError,match='extra'):
        module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=cache)
    assert not (payload/module.MEMBER).exists()


@pytest.mark.parametrize('requirements,locked',[
    (b'demo==1\n',b'demo==1 \\\n    --hash=sha256:'+b'a'*64+b'\nextra==1 \\\n    --hash=sha256:'+b'b'*64+b'\n'),
    (b'demo==1\n',b'demo==2 \\\n    --hash=sha256:'+b'a'*64+b'\n'),
    (b'demo>=1\n',b'demo==1 \\\n    --hash=sha256:'+b'a'*64+b'\n'),
    (b'demo==1\n',b'demo==1 \\\n    --hash=sha256:'+b'a'*64+b' \\\n    --hash=sha256:'+b'a'*64+b'\n'),
])
def test_whole_source_declarations_are_retained_or_invalid_rows_rejected(requirements,locked):
    if b'extra==1' in locked:
        result=module.plugin_dependency_declarations(requirements,locked,policy(),python_version='3.12.14')
        assert len(result['locked'])==2  # Never silently trim a locked transitive row.
    else:
        with pytest.raises(ValueError):
            module.plugin_dependency_declarations(requirements,locked,policy(),python_version='3.12.14')


def cpu_source(url=None,hash_pin='a'*64):
    url=url or 'https://download-r2.pytorch.org/whl/cpu/torch-2.14.1%2Bcpu-cp312-cp312-win_amd64.whl#sha256='+hash_pin
    value=policy()
    value['direct_wheels']={'torch':{'url':url,'sha256':hash_pin}}
    requirement=('torch @ '+url).encode()
    return requirement+b'\n',requirement+b' \\\n    --hash=sha256:'+hash_pin.encode()+b'\n',value


def test_exact_cpu_supplier_url_and_two_hash_declarations_have_a_separate_policy():
    roots,locked,value=cpu_source()
    result=module.plugin_dependency_declarations(roots,locked,value,python_version='3.12.14')
    assert result['roots']==['torch==2.14.1+cpu']
    assert result['locked'][0]['direct_sha256']=='a'*64
    with pytest.raises(ValueError):
        canonical_requirements(locked)  # The generic host URL prohibition is unchanged.


@pytest.mark.parametrize('change',['supplier','credentials','scheme','ABI','fragment','query'])
def test_direct_wheel_policy_rejects_wrong_supplier_credentials_abi_and_hash(change):
    roots,locked,value=cpu_source()
    original=value['direct_wheels']['torch']['url']
    replacements={'supplier':('download-r2.pytorch.org','localhost'), 'credentials':('https://','https://user:secret@'),
        'scheme':('https://','http://'),'ABI':('cp312-cp312','cp313-cp313'),
        'fragment':('sha256='+('a'*64),'sha256='+('b'*64)), 'query':('#sha256=','?extra=1#sha256=')}
    old,new=replacements[change]
    altered=original.replace(old,new)
    roots,locked,value=cpu_source(altered)
    with pytest.raises((ValueError,InstallError)):
        module.plugin_dependency_declarations(roots,locked,value,python_version='3.12.14')


def test_foreign_policy_cannot_adopt_plugin_artifacts(inputs):
    payload,manifest,_cache=inputs
    manifest.pop(module.LOCK)
    (payload/module.MEMBER).write_bytes(b'{}')
    with pytest.raises(ValueError,match='own frozen source policy'):
        module.verify_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml')


def test_direct_digest_disagreement_cannot_be_fixed_by_rehashing_a_report():
    roots,locked,value=cpu_source()
    changed=locked.replace(b'--hash=sha256:'+b'a'*64,b'--hash=sha256:'+b'b'*64)
    with pytest.raises(ValueError,match='hash differs'):
        module.plugin_dependency_declarations(roots,changed,deepcopy(value),python_version='3.12.14')


def test_known_source_build_cannot_be_omitted_to_reach_a_legacy_build_path():
    locked=b'langdetect==1.0.9 \\\n    --hash=sha256:'+b'a'*64+b'\n'
    with pytest.raises(ValueError,match='source-build admission cannot be omitted'):
        module.plugin_dependency_declarations(b'langdetect==1.0.9\n',locked,policy(),python_version='3.12.14')


def test_cache_changes_after_authentication_cannot_relabel_the_copied_bytes(inputs,monkeypatch):
    payload,manifest,cache=inputs
    original=module._cached
    def replace_listing(source,report):
        values=original(source,report)
        before=cache/'demo_pkg-1.0-py3-none-any.whl'
        after=cache/'foreign-1.0-py3-none-any.whl'
        assert before.resolve().parent==after.resolve().parent==cache.resolve()
        before.rename(after)
        return values
    with monkeypatch.context() as context:
        context.setattr(module,'_cached',replace_listing)
        module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=cache)
    assert (cache/'foreign-1.0-py3-none-any.whl').exists()
    assert (payload/module.WHEELS/'demo_pkg-1.0-py3-none-any.whl').is_file()
    assert not (payload/module.WHEELS/'foreign-1.0-py3-none-any.whl').exists()
    assert module.verify_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml')['wheel_closure_verified']


@pytest.fixture
def paired_inputs(tmp_path,monkeypatch):
    payload,manifest,metadata=base_payload.__wrapped__(tmp_path,monkeypatch)
    cache=tmp_path/'original-wheels'
    cache.mkdir()
    demo=dependency_wheel('demo-pkg',python='>=3.12,<3.13')
    (cache/'demo_pkg-1.0-py3-none-any.whl').write_bytes(demo.data)
    raw,original,pins=paired()
    six=dependency_wheel('six')
    source_policy=policy()
    source_policy.update(schema=module.COPY_SCHEMA,recipe='windows-vtuber-original-inputs/v2',wheel_archive_policy=module.ARCHIVE_POLICY,source_builds={
        'langdetect':{'version':'1.0.9','state':module.COPY_STATE,'recipe':module.COPY_RECIPE,
            'source_sha256':pins['expected_source_sha256'],'original_wheel_sha256':original.sha256,
            'setup_sha256':pins['expected_setup_sha256']}})
    frozen(payload,manifest,module.LOCK,encoded(source_policy))
    frozen(payload,manifest,module._SOURCES['requirements_input'],b'demo-pkg==1.0\nlangdetect==1.0.9\n')
    for name in ['upstream','artifacts']:
        frozen(payload,manifest,module._SOURCES[name],encoded({'backend':{},'frontend':{}}))
    lines=[]
    for name,hashes in [('demo-pkg==1.0',[demo.sha256]),('langdetect==1.0.9',[original.sha256,digest(raw)]),('six==1.0',[six.sha256])]:
        lines.append(name+' \\\n'+' \\\n'.join('    --hash=sha256:'+h for h in hashes)+'\n')
    frozen(payload,manifest,module._SOURCES['requirements_lock'],''.join(lines).encode())
    (cache/'langdetect-1.0.9-py2-none-any.whl').write_bytes(original.data)
    (cache/'six-1.0-py3-none-any.whl').write_bytes(six.data)
    sources=tmp_path/'paired-sources'
    sources.mkdir()
    (sources/module.SOURCE_FILENAME).write_bytes(raw)
    prepare_native_python_inputs(payload,manifest,metadata=metadata)
    return payload,manifest,cache,sources


def test_paired_original_inputs_bind_production_scope_reader_and_data_only_transform(paired_inputs):
    payload,manifest,wheels,sources=paired_inputs
    members=module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=wheels,source_cache=sources)
    value=module.verify_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',members)
    assert value['schema']=='agentstrata-windows-plugin-input-audit/v2' and value['wheel_closure_verified']
    assert len(value['wheel_archives'])==3 and len(value['source_archives'])==len(value['wheel_transforms'])==1
    transform=value['wheel_transforms'][0]
    raw=next(row for row in value['wheel_archives'] if row['name']=='langdetect')
    assert transform['original_wheel_sha256']==raw['sha256'] and transform['output_sha256']!=raw['sha256']
    assert not any((transform['target_programs_executed'],transform['python_runtime_verified'],transform['installation_accepted']))
    assert not any('source-build-not-admitted' in blocker for blocker in value['blockers'])
    assert not value['installation_accepted'] and not value['prefix_loading_abi_verified']
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    required=json.loads((payload/REQUIREMENTS).read_bytes())
    scope=next(row for row in required['scopes'] if row['id']=='plugin-components')
    source_member=module.SOURCES+'/'+module.SOURCE_FILENAME
    assert scope['inputs'][source_member]==members[source_member] and required['state']=='incomplete'
    package(payload,manifest)
    assert read_bundle(payload)['bot_id']=='demo'


@pytest.mark.parametrize('change',['missing','extra','tampered','wrong-root'])
def test_paired_source_cache_rejects_missing_extra_tampered_or_foreign_inputs_before_outputs(paired_inputs,change):
    payload,manifest,wheels,sources=paired_inputs
    path=sources/module.SOURCE_FILENAME
    if change=='missing':
        path.unlink()
    elif change=='extra':
        (sources/'build.json').write_text('{"accepted":true}')
    elif change=='tampered':
        path.write_bytes(path.read_bytes()+b'altered')
    else:
        path.rename(sources/'foreign.tar.gz')
    with pytest.raises(ValueError):
        module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=wheels,source_cache=sources)
    assert not (payload/module.MEMBER).exists() and not (payload/'plugin-wheels').exists() and not (payload/'plugin-sources').exists()


def test_complete_wheels_without_original_source_cannot_admit_the_conversion(paired_inputs):
    payload,manifest,wheels,_sources=paired_inputs
    with pytest.raises(ValueError,match='original source cache'):
        module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=wheels)
    assert not (payload/module.MEMBER).exists()


@pytest.mark.parametrize('change',['source-bytes','source-omitted','conversion-output','accepted'])
def test_catalog_rehash_cannot_replace_paired_originals_or_promote_a_transform(paired_inputs,tmp_path,change):
    payload,manifest,wheels,sources=paired_inputs
    module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=wheels,source_cache=sources)
    prepare_runtime_requirements(payload,manifest,'bots/demo/bot.yaml','demo')
    source=payload/module.SOURCES/module.SOURCE_FILENAME
    report=json.loads((payload/module.MEMBER).read_bytes())
    if change=='source-bytes':
        source.write_bytes(source.read_bytes()+b'altered')
    elif change=='source-omitted':
        source.unlink()
    elif change=='conversion-output':
        report['wheel_transforms'][0]['output_sha256']='0'*64
    else:
        report['wheel_transforms'][0]['installation_accepted']=True
    (payload/module.MEMBER).write_bytes(encoded(report))
    package(payload,manifest)
    target=tmp_path/'never-install'
    with pytest.raises((ValueError,InstallError)):
        install_bundle(payload,target)
    assert not target.exists()


def test_local_generated_wheel_is_not_an_original_input_even_with_a_build_receipt(paired_inputs):
    from chatcopilot.core.langdetect_wheel import langdetect_wheel_copy
    payload,manifest,wheels,sources=paired_inputs
    raw,original,pins=paired()
    generated=langdetect_wheel_copy(raw,original,**pins)
    (wheels/'langdetect-1.0.9-py2-none-any.whl').write_bytes(generated.wheel.data)
    (payload/'build.json').write_bytes(encoded({'accepted':True,'wheel_sha256':generated.wheel.sha256}))
    with pytest.raises(ValueError,match='absent from the frozen'):
        module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=wheels,source_cache=sources)
    assert not (payload/module.MEMBER).exists()


def test_authenticated_source_bytes_keep_their_canonical_destination_after_cache_rename(paired_inputs,monkeypatch):
    payload,manifest,wheels,sources=paired_inputs
    original=module._cached_sources
    def replace_listing(cache,report):
        values=original(cache,report)
        (sources/module.SOURCE_FILENAME).rename(sources/'foreign.tar.gz')
        return values
    with monkeypatch.context() as context:
        context.setattr(module,'_cached_sources',replace_listing)
        module.prepare_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml',wheel_cache=wheels,source_cache=sources)
    assert (sources/'foreign.tar.gz').is_file()
    assert (payload/module.SOURCES/module.SOURCE_FILENAME).is_file()
    assert not (payload/module.SOURCES/'foreign.tar.gz').exists()
    assert module.verify_native_plugin_inputs(payload,manifest,'bots/demo/bot.yaml')['wheel_closure_verified']
