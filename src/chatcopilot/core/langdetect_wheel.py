"""Deterministic data conversion of paired, independently pinned langdetect inputs.

The upstream setup.py is authenticated but never imported or run. The source and
original wheel must contain identical Python/profile bytes. Changing WHEEL/RECORD
is an owned packaging recipe, not an upstream Python-3 wheel or runtime approval.
"""
from __future__ import annotations

import base64
import csv
from dataclasses import dataclass
import hashlib
import io
import re
import tarfile
import zipfile

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.archive_inventory import ZipInventoryLimits
from chatcopilot.core.tar_inventory import TarInventoryLimits, tar_inventory
from chatcopilot.core.wheel_plan import PinnedWheel, WheelPlanLimits, _headers, _one, _values, audit_windows_wheel_inputs

RECIPE = "langdetect-paired-source-wheel-copy/v1"
SOURCE_FILENAME = "langdetect-1.0.9.tar.gz"
ORIGINAL_FILENAME = "langdetect-1.0.9-py2-none-any.whl"
FILENAME = "langdetect-1.0.9-py3-none-any.whl"
_PREFIX = "langdetect-1.0.9/"
_INFO = "langdetect-1.0.9.dist-info/"
_MIB = 1024 * 1024
_LIMITS = TarInventoryLimits(max_archive_bytes=2*_MIB, max_stream_bytes=16*_MIB,
    max_members=256, max_tree_entries=512, max_member_bytes=2*_MIB, max_total_bytes=8*_MIB,
    max_path_bytes=256, max_depth=8)
_WHEEL_LIMITS = WheelPlanLimits(archive=ZipInventoryLimits(max_archive_bytes=4*_MIB,
    max_directory_bytes=128*1024, max_members=256, max_tree_entries=512,
    max_member_bytes=2*_MIB, max_total_bytes=8*_MIB, max_path_bytes=256, max_depth=8))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class LangdetectWheelCopy:
    wheel: PinnedWheel
    source_archive_sha256: str
    original_wheel_sha256: str
    setup_sha256: str
    package_files: tuple[tuple[str, int, str], ...]
    recipe: str = RECIPE
    filename: str = FILENAME
    target_programs_executed: bool = False
    python_runtime_verified: bool = False
    installation_accepted: bool = False

    def audit(self):
        return {"recipe":self.recipe,"name":"langdetect","version":"1.0.9",
            "source_archive_sha256":self.source_archive_sha256,"original_wheel_sha256":self.original_wheel_sha256,
            "setup_sha256":self.setup_sha256,"output_sha256":self.wheel.sha256,"output_bytes":len(self.wheel.data),
            "output_filename":self.filename,"package_files":[{"path":p,"bytes":n,"sha256":h} for p,n,h in self.package_files],
            "source_and_original_wheel_package_bytes_equal":True,"declared_python3":True,
            "target_programs_executed":False,"python_runtime_verified":False,"installation_accepted":False}


def langdetect_wheel_copy(source: bytes, original: PinnedWheel, *, expected_source_sha256: str,
                         expected_original_sha256: str, expected_setup_sha256: str) -> LangdetectWheelCopy:
    """Build an inert owned container from a declared source pair; no disk or subprocess access."""
    pins=(expected_source_sha256,expected_original_sha256,expected_setup_sha256)
    if (any(type(pin) is not str or re.fullmatch(r"[a-f0-9]{64}",pin) is None for pin in pins)
            or type(source) is not bytes or type(original) is not PinnedWheel
            or type(original.data) is not bytes or len(original.data)>2*_MIB
            or original.name!="langdetect" or original.version!="1.0.9"
            or original.sha256!=expected_original_sha256 or _sha(original.data)!=expected_original_sha256):
        raise InstallError("Langdetect conversion requires independently pinned original source and wheel identities")
    inventory=tar_inventory(source,expected_sha256=expected_source_sha256,limits=_LIMITS)
    source_files={file.path:file for file in inventory.files}
    if any(not name.startswith(_PREFIX) for name in (*source_files,*inventory.directories) if name!=_PREFIX.rstrip('/')):
        raise InstallError("Langdetect source requires its exact version root")
    audit_windows_wheel_inputs((original,),compatible_tags=("py2-none-any",),limits=_WHEEL_LIMITS)
    selected={}
    seen=set()
    with tarfile.open(fileobj=io.BytesIO(source),mode="r|gz",encoding="ascii",errors="strict") as archive:
        for item in archive:
            if item.isdir():
                if item.name.rstrip('/') not in inventory.directories or item.size:
                    raise InstallError("Langdetect TAR directory differs from strict inventory")
                continue
            if not item.isfile() or item.name not in source_files or item.name in seen:
                raise InstallError("Langdetect TAR member differs from strict inventory")
            seen.add(item.name)
            file=source_files[item.name]
            stream=archive.extractfile(item)
            if stream is None or item.size!=file.size:
                raise InstallError("Langdetect TAR file size differs from strict inventory")
            with stream:
                raw=stream.read(file.size+1)
            if len(raw)!=file.size or _sha(raw)!=file.sha256:
                raise InstallError("Langdetect TAR contents differ from strict inventory")
            name=item.name.removeprefix(_PREFIX)
            if name.startswith('langdetect/') or name in {'setup.py','PKG-INFO','langdetect.egg-info/requires.txt'}:
                selected[name]=raw
    if seen!=set(source_files):
        raise InstallError("Langdetect TAR reader omitted strictly inventoried files")
    if (not {'setup.py','PKG-INFO','langdetect.egg-info/requires.txt'}.issubset(selected)
            or _sha(selected['setup.py'])!=expected_setup_sha256
            or selected['langdetect.egg-info/requires.txt']!=b'six\n'):
        raise InstallError("Langdetect source setup or static dependency declaration differs")
    info=_headers(selected['PKG-INFO'])
    if (_one(info,'Metadata-Version')!='2.1' or _one(info,'Name')!='langdetect' or _one(info,'Version')!='1.0.9'
            or 'Programming Language :: Python :: 3' not in _values(info,'Classifier')
            or _values(info,'Requires-Dist') not in ([],['six']) or _values(info,'Requires-Python')
            or _values(info,'Dynamic') or _values(info,'Provides-Extra')):
        raise InstallError("Langdetect source does not declare the reviewed Python-3 project")
    package={name:raw for name,raw in selected.items() if name.startswith('langdetect/')}
    if (not package or 'langdetect/__init__.py' not in package or len(package)>128
            or any(re.fullmatch(r'langdetect/(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*\.py',name) is None
                and re.fullmatch(r'langdetect/profiles/[a-z]{2,3}(?:-[a-z]{2})?',name) is None
                and name!='langdetect/utils/messages.properties' for name in package)):
        raise InstallError("Langdetect package contains unsupported loading paths or files")
    with zipfile.ZipFile(io.BytesIO(original.data)) as archive:
        values={name:archive.read(name) for name in archive.namelist() if not name.endswith('/')}
    wheel_package={name:raw for name,raw in values.items() if name.startswith('langdetect/')}
    metadata={_INFO+name for name in ['LICENSE','NOTICE','METADATA','WHEEL','top_level.txt','RECORD']}
    if wheel_package!=package or set(values)-set(package)!=metadata:
        raise InstallError("Langdetect source and original wheel do not have the same reviewed package files")
    info=_headers(values[_INFO+'METADATA'])
    if (_one(info,'Name')!='langdetect' or _one(info,'Version')!='1.0.9'
            or _values(info,'Requires-Dist')!=['six'] or _values(info,'Requires-Python')
            or _values(info,'Dynamic') or _values(info,'Provides-Extra')
            or 'Programming Language :: Python :: 3' not in _values(info,'Classifier')):
        raise InstallError("Langdetect original metadata differs from its static source recipe")
    values[_INFO+'WHEEL']=(f'Wheel-Version: 1.0\nGenerator: agentstrata-{RECIPE}\n'
        'Root-Is-Purelib: true\nTag: py3-none-any\n').encode()
    values.pop(_INFO+'RECORD')
    record=io.StringIO(newline='')
    writer=csv.writer(record,lineterminator='\n')
    for name,raw in sorted(values.items()):
        pin=base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode()
        writer.writerow((name,'sha256='+pin,str(len(raw))))
    writer.writerow((_INFO+'RECORD','',''))
    values[_INFO+'RECORD']=record.getvalue().encode()
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as archive:
        for name,raw in sorted(values.items()):
            entry=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
            entry.create_system=3
            entry.external_attr=0o100644<<16
            archive.writestr(entry,raw)
    raw=output.getvalue()
    wheel=PinnedWheel(raw,_sha(raw),'langdetect','1.0.9')
    audit_windows_wheel_inputs((wheel,),compatible_tags=('py3-none-any',),limits=_WHEEL_LIMITS)
    return LangdetectWheelCopy(wheel,inventory.archive_sha256,original.sha256,expected_setup_sha256,
        tuple((name,len(raw),_sha(raw)) for name,raw in sorted(package.items())))
