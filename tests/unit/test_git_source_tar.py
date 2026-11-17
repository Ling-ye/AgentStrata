"""Git source metadata is explicit data, never a generic TAR exception or execution authority."""
import gzip
import hashlib
import subprocess
import tarfile

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.tar_inventory import TarInventoryLimits,git_source_tar_inventory,read_git_source_members,tar_inventory
from tests.unit.test_tar_inventory import archive,member,pax_record

REV='a'*40
ROOT='source-'+REV


def fixture(*,global_content=None,global_name='pax_global_header',extra=(),leading=True):
    comment=member(global_name,global_content if global_content is not None else pax_record(b'comment',REV.encode()),kind=tarfile.XGLTYPE)
    files=(member(ROOT+'/pyproject.toml',b'[project]\nname="inert"\n'),member(ROOT+'/uv.lock',b'version = 1\n'),
        member(ROOT+'/inert.py',b"raise RuntimeError('source must never execute')\n"))
    return gzip.compress(archive(*((comment,) if leading else ()),*files,*extra),mtime=0)


def inspect(raw,**options):
    return git_source_tar_inventory(raw,expected_sha256=hashlib.sha256(raw).hexdigest(),expected_revision=REV,**options)


def test_complete_git_comment_inventory_is_opt_in_and_original_runtime_policy_stays_closed():
    raw=fixture()
    with pytest.raises(InstallError,match='forbidden'):
        tar_inventory(raw,expected_sha256=hashlib.sha256(raw).hexdigest())
    result=inspect(raw)
    assert result.schema=='agentstrata-git-source-tar-inventory/v1' and result.members==4
    assert len(result.files)==3 and result.archive_sha256==hashlib.sha256(raw).hexdigest()
    assert all(item.path.startswith(ROOT+'/') for item in result.files)


@pytest.mark.parametrize('change',['wrong-revision','path-field','extra-field','duplicate','misnamed','missing','second-global','link'])
def test_git_scope_cannot_add_global_paths_unknown_fields_repeated_records_or_links(change):
    options={}
    if change=='wrong-revision':
        options['global_content']=pax_record(b'comment',b'b'*40)
    elif change=='path-field':
        options['global_content']=pax_record(b'path',b'../../escape')
    elif change=='extra-field':
        options['global_content']=pax_record(b'comment',REV.encode())+pax_record(b'mtime',b'1')
    elif change=='duplicate':
        options['global_content']=pax_record(b'comment',REV.encode())*2
    elif change=='misnamed':
        options['global_name']='../pax_global_header'
    elif change=='missing':
        options['leading']=False
    elif change=='second-global':
        options['extra']=(member('pax_global_header',pax_record(b'comment',REV.encode()),kind=tarfile.XGLTYPE),)
    elif change=='link':
        options['extra']=(member(ROOT+'/link',b'',kind=tarfile.SYMTYPE,link='../../outside'),)
    with pytest.raises(InstallError):
        inspect(fixture(**options))


@pytest.mark.parametrize('revision',[None,'A'*40,'a'*39,'../escape',True])
def test_revision_identity_is_closed(revision):
    raw=fixture()
    with pytest.raises(InstallError,match='revision'):
        git_source_tar_inventory(raw,expected_sha256=hashlib.sha256(raw).hexdigest(),expected_revision=revision)


def test_source_pin_precedes_any_reader_or_metadata_interpretation(monkeypatch):
    import chatcopilot.core.tar_inventory as module
    monkeypatch.setattr(module,'_Reader',lambda *_a,**_k:pytest.fail('parsed before independent pin'))
    with pytest.raises(InstallError,match='SHA256 differs'):
        git_source_tar_inventory(fixture(),expected_sha256='0'*64,expected_revision=REV)


def test_selected_metadata_is_read_in_memory_and_all_source_bytes_rechecked(tmp_path,monkeypatch):
    monkeypatch.setattr(tarfile.TarFile,'extract',lambda *_a,**_k:pytest.fail('filesystem extraction'))
    monkeypatch.setattr(tarfile.TarFile,'extractall',lambda *_a,**_k:pytest.fail('filesystem extraction'))
    monkeypatch.setattr(subprocess,'Popen',lambda *_a,**_k:pytest.fail('source execution'))
    raw=fixture()
    inventory,selected=read_git_source_members(raw,expected_sha256=hashlib.sha256(raw).hexdigest(),expected_revision=REV,
        members=(ROOT+'/pyproject.toml',ROOT+'/uv.lock'))
    assert len(inventory.files)==3 and selected[ROOT+'/uv.lock']==b'version = 1\n'
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('members,budget',[((ROOT+'/uv.lock',ROOT+'/uv.lock'),4096),(('../escape',),4096),((ROOT+'/uv.lock',),1),([],4096)])
def test_metadata_selection_cannot_escape_or_exceed_budget(members,budget):
    raw=fixture()
    with pytest.raises(InstallError,match='selection'):
        read_git_source_members(raw,expected_sha256=hashlib.sha256(raw).hexdigest(),expected_revision=REV,members=members,max_selected_bytes=budget)


def test_git_comment_counts_against_existing_metadata_and_member_budgets():
    with pytest.raises(InstallError):
        inspect(fixture(),limits=TarInventoryLimits(max_metadata_bytes=51))
    with pytest.raises(InstallError):
        inspect(fixture(),limits=TarInventoryLimits(max_members=3))
