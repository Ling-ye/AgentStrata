"""Plugin-local redundant sizes cannot enable general ZIP64 or relax host inputs."""
import hashlib
import io
import struct
import zipfile

import pytest

from chatcopilot.core.archive_inventory import plugin_zip_inventory, zip_inventory
from chatcopilot.core.dependency_closure import DependencyClosureLimits, windows_dependency_input_inventory, windows_plugin_dependency_input_inventory
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.wheel_plan import PinnedWheel
from tests.unit.test_dependency_closure import wheel


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def redundant_local_sizes(files):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        for name,raw in sorted(files.items()):
            item=zipfile.ZipInfo(name,(2020,1,1,0,0,0))
            item.create_system=3
            item.external_attr=0o100600<<16
            item.compress_type=zipfile.ZIP_DEFLATED
            with archive.open(item,'w',force_zip64=True) as stream:
                stream.write(raw)
    data=bytearray(output.getvalue())
    with zipfile.ZipFile(output) as archive:
        for item in archive.infolist():
            struct.pack_into('<II',data,item.header_offset+18,item.compress_size,item.file_size)
    return bytes(data)


def extra_start(raw):
    n=struct.unpack_from('<H',raw,26)[0]
    return 30+n


def test_exact_redundant_local_sizes_are_plugin_data_only():
    raw=redundant_local_sizes({'demo.py':b'raise RuntimeError("Never run")\n'})
    with pytest.raises(InstallError,match='extra metadata'):
        zip_inventory(raw,expected_sha256=digest(raw))
    result=plugin_zip_inventory(raw,expected_sha256=digest(raw))
    assert result.schema=='agentstrata-plugin-zip-inputs/v1'
    assert [(x.path,x.size) for x in result.files]==[('demo.py',32)]


@pytest.mark.parametrize('changed',['uncompressed','compressed','length','sentinel','streaming','crc'])
def test_redundant_local_fields_cannot_override_the_ordinary_headers(changed):
    raw=bytearray(redundant_local_sizes({'demo.py':b'inert\n'}))
    start=extra_start(raw)
    if changed in {'uncompressed','compressed'}:
        offset=start+4+(8 if changed=='compressed' else 0)
        value=struct.unpack_from('<Q',raw,offset)[0]
        struct.pack_into('<Q',raw,offset,value+1)
    elif changed=='length':
        struct.pack_into('<H',raw,start+2,8)
    elif changed=='sentinel':
        struct.pack_into('<II',raw,18,0xffffffff,0xffffffff)
    elif changed=='streaming':
        struct.pack_into('<H',raw,6,8)
    else:
        struct.pack_into('<I',raw,14,0)
    with pytest.raises(InstallError):
        plugin_zip_inventory(bytes(raw),expected_sha256=digest(raw))


def test_duplicate_local_zip64_size_fields_are_rejected():
    original=redundant_local_sizes({'demo.py':b'inert\n'})
    start=extra_start(original)
    extra=original[start:start+20]
    raw=bytearray(original[:start+20]+extra+original[start+20:])
    struct.pack_into('<H',raw,28,40)
    eocd=len(raw)-22
    old_directory=struct.unpack_from('<I',raw,eocd+16)[0]
    struct.pack_into('<I',raw,eocd+16,old_directory+20)
    with pytest.raises(InstallError,match='repeat'):
        plugin_zip_inventory(bytes(raw),expected_sha256=digest(raw))


def test_central_zip64_metadata_stays_forbidden():
    original=redundant_local_sizes({'demo.py':b'inert\n'})
    eocd=len(original)-22
    directory=struct.unpack_from('<I',original,eocd+16)[0]
    name_size,extra_size=struct.unpack_from('<HH',original,directory+28)
    assert extra_size==0
    extra=original[extra_start(original):extra_start(original)+20]
    where=directory+46+name_size
    raw=bytearray(original[:where]+extra+original[where:])
    struct.pack_into('<H',raw,directory+30,len(extra))
    struct.pack_into('<I',raw,eocd+20+12,struct.unpack_from('<I',raw,eocd+20+12)[0]+20)
    with pytest.raises(InstallError,match='extra metadata'):
        plugin_zip_inventory(bytes(raw),expected_sha256=digest(raw))


def test_plugin_wheel_scope_binds_original_digest_without_enabling_host_or_runtime():
    pin=wheel('demo-pkg',python='>=3.12,<3.13')
    with zipfile.ZipFile(io.BytesIO(pin.data)) as old:
        raw=redundant_local_sizes({name:old.read(name) for name in old.namelist()})
    pin=PinnedWheel(raw,digest(raw),'demo-pkg','1.0')
    with pytest.raises(InstallError):
        windows_dependency_input_inventory((pin,),python_version='3.12.14',requirements=('demo-pkg==1.0',))
    result=windows_plugin_dependency_input_inventory((pin,),python_version='3.12.14',requirements=('demo-pkg==1.0',))
    assert result.inventory.schema=='agentstrata-windows-plugin-wheel-input-inventory/v1'
    assert result.closure.nodes[0].archive_sha256==pin.sha256
    assert result.closure.scope=='pinned-windows-plugin-distributions' and not result.closure.native_abi_verified
    with pytest.raises(InstallError,match='Python 3.12'):
        windows_plugin_dependency_input_inventory((pin,),python_version='3.13.15',requirements=('demo-pkg==1.0',))


def test_plugin_roots_have_a_separate_finite_budget_and_host_default_is_preserved():
    pins=tuple(wheel(f'root-{n}') for n in range(129))
    roots=tuple(f'root-{n}==1.0' for n in range(129))
    with pytest.raises(InstallError,match='bounded immutable root'):
        windows_dependency_input_inventory(pins,python_version='3.12.14',requirements=roots)
    result=windows_plugin_dependency_input_inventory(pins,python_version='3.12.14',requirements=roots)
    assert len(result.closure.nodes)==129
    with pytest.raises(InstallError,match='bounded policies'):
        windows_plugin_dependency_input_inventory(pins,python_version='3.12.14',requirements=roots,
            limits=DependencyClosureLimits(max_roots=257))


def test_plugin_extra_capacity_keeps_all_declarations_and_still_rejects_aliases():
    pin=wheel('demo-pkg',extras=tuple(f'feature-{n}' for n in range(40)))
    with pytest.raises(InstallError,match='extras'):
        windows_dependency_input_inventory((pin,),python_version='3.12.14',requirements=('demo-pkg==1.0',))
    result=windows_plugin_dependency_input_inventory((pin,),python_version='3.12.14',requirements=('demo-pkg==1.0',))
    assert len(result.closure.nodes[0].provided_extras)==40
    with pytest.raises(InstallError,match='bounded policies'):
        windows_plugin_dependency_input_inventory((pin,),python_version='3.12.14',requirements=('demo-pkg==1.0',),
            limits=DependencyClosureLimits(max_extras_per_wheel=65))
    ambiguous=wheel('demo-pkg',extras=('same-name','same_name'))
    with pytest.raises(InstallError,match='ambiguous'):
        windows_plugin_dependency_input_inventory((ambiguous,),python_version='3.12.14',requirements=('demo-pkg==1.0',))
