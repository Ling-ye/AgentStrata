"""Owned metadata conversion requires a complete original source pair, never execution."""
from dataclasses import replace
import gzip
import io
import subprocess
import zipfile

import pytest

from chatcopilot.core.dependency_closure import windows_dependency_input_inventory
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.langdetect_wheel import RECIPE, langdetect_wheel_copy
from chatcopilot.core.wheel_plan import PinnedWheel
from tests.unit.test_dependency_closure import wheel as dependency_wheel
from tests.unit.test_tar_inventory import archive, member
from tests.unit.test_wheel_plan import digest, wheel

DIST='langdetect-1.0.9.dist-info'
METADATA=(b'Metadata-Version: 2.1\nName: langdetect\nVersion: 1.0.9\n'
          b'Classifier: Programming Language :: Python :: 3\nRequires-Dist: six\n\n')
HEADERS=b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py2-none-any\n\n'


def paired(*, source_extra=None, wheel_extra=None, source_requires=b'six\n', metadata=METADATA, python3=True):
    files={'langdetect/__init__.py':b'raise RuntimeError("Never import original package")\n',
           'langdetect/profiles/en':b'{"name":"en","freq":{}}\n'}
    source={**files,'setup.py':b'raise RuntimeError("Never execute setup.py")\n',
            'PKG-INFO':METADATA if python3 else METADATA.replace(b'Classifier: Programming Language :: Python :: 3\n',b''),
            'langdetect.egg-info/requires.txt':source_requires}
    source.update(source_extra or {})
    raw=gzip.compress(archive(*(member('langdetect-1.0.9/'+name,data) for name,data in sorted(source.items()))),mtime=0)
    extra={**files,**{DIST+'/'+name:b'Original attribution\n' for name in ['LICENSE','NOTICE']},DIST+'/top_level.txt':b'langdetect\n'}
    extra.update(wheel_extra or {})
    zipped=wheel(extra,metadata=metadata,headers=HEADERS,dist=DIST,remove=('demo_pkg/__init__.py',))
    original=PinnedWheel(zipped,digest(zipped),'langdetect','1.0.9')
    pins={'expected_source_sha256':digest(raw),'expected_original_sha256':original.sha256,
          'expected_setup_sha256':digest(source['setup.py'])}
    return raw,original,pins


@pytest.fixture(autouse=True)
def no_program(monkeypatch):
    monkeypatch.setattr(subprocess,'Popen',lambda *_a,**_k:pytest.fail('Source metadata conversion executed a program'))


def test_deterministic_source_pair_closes_metadata_without_installation_or_execution(tmp_path):
    raw,original,pins=paired()
    copied=langdetect_wheel_copy(raw,original,**pins)
    assert copied==langdetect_wheel_copy(raw,original,**pins)
    assert copied.recipe==RECIPE and copied.original_wheel_sha256==original.sha256
    assert copied.wheel.sha256!=original.sha256 and copied.source_archive_sha256==pins['expected_source_sha256']
    assert len(copied.package_files)==2
    assert not any((copied.target_programs_executed,copied.python_runtime_verified,copied.installation_accepted))
    assert not list(tmp_path.iterdir())
    with zipfile.ZipFile(io.BytesIO(copied.wheel.data)) as z,zipfile.ZipFile(io.BytesIO(original.data)) as old:
        assert b'Tag: py3-none-any\n' in z.read(DIST+'/WHEEL')
        for name in z.namelist():
            if name not in {DIST+'/WHEEL',DIST+'/RECORD'}:
                assert z.read(name)==old.read(name)
    result=windows_dependency_input_inventory((copied.wheel,dependency_wheel('six')),python_version='3.12.14',requirements=('langdetect==1.0.9',))
    assert {n.name for n in result.closure.nodes}=={'langdetect','six'}
    assert next(n.archive_sha256 for n in result.closure.nodes if n.name=='langdetect')==copied.wheel.sha256


def test_original_py2_input_stays_rejected_by_general_python3_policy():
    _raw,original,_pins=paired()
    with pytest.raises(InstallError):
        windows_dependency_input_inventory((original,dependency_wheel('six')),python_version='3.12.14',requirements=('langdetect==1.0.9',))


@pytest.mark.parametrize('field',['expected_source_sha256','expected_original_sha256','expected_setup_sha256'])
def test_independent_source_wheel_and_reviewed_setup_pins_are_required(field):
    raw,original,pins=paired()
    pins[field]='0'*64
    with pytest.raises(InstallError):
        langdetect_wheel_copy(raw,original,**pins)


@pytest.mark.parametrize('changed',['name','version','digest'])
def test_original_wheel_identity_cannot_be_relabelled(changed):
    raw,original,pins=paired()
    original=replace(original,**{'name':'other'} if changed=='name' else {'version':'2.0'} if changed=='version' else {'sha256':'0'*64})
    with pytest.raises(InstallError):
        langdetect_wheel_copy(raw,original,**pins)


@pytest.mark.parametrize('options',[
    {'source_extra':{'langdetect/__init__.py':b'altered\n'}},
    {'source_requires':b'six\nrequests\n'},
    {'python3':False},
    {'source_extra':{'PKG-INFO':METADATA.replace(b'Requires-Dist: six',b'Requires-Dist: requests')}},
    {'source_extra':{'PKG-INFO':METADATA.replace(b'Requires-Dist: six',b'Requires-Dist: six\nRequires-Python: <3')}},
    {'metadata':METADATA.replace(b'Requires-Dist: six',b'Requires-Dist: requests')},
    {'metadata':METADATA.replace(b'Requires-Dist: six',b'Requires-Dist: six\nRequires-Python: <3')},
    {'source_extra':{'langdetect/startup.pth':b'import attacker'},'wheel_extra':{'langdetect/startup.pth':b'import attacker'}},
    {'wheel_extra':{'foreign/__init__.py':b'raise RuntimeError()'}},
    {'wheel_extra':{DIST+'/entry_points.txt':b'[console_scripts]\ninject=evil:main\n'}},
])
def test_source_metadata_loading_paths_and_package_mismatches_are_rejected(options):
    raw,original,pins=paired(**options)
    with pytest.raises(InstallError):
        langdetect_wheel_copy(raw,original,**pins)


def test_matching_source_pair_still_requires_all_dependency_providers():
    raw,original,pins=paired()
    copied=langdetect_wheel_copy(raw,original,**pins)
    with pytest.raises(InstallError,match='missing|absent'):
        windows_dependency_input_inventory((copied.wheel,),python_version='3.12.14',requirements=('langdetect==1.0.9',))


def test_compressed_package_member_cannot_exceed_the_dedicated_byte_budget():
    large=b'x'*(2*1024*1024+1)
    raw,original,pins=paired(source_extra={'langdetect/profiles/en':large},wheel_extra={'langdetect/profiles/en':large})
    with pytest.raises(InstallError,match='limit|budget'):
        langdetect_wheel_copy(raw,original,**pins)
