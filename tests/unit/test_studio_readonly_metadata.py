"""Studio inspects original Harness sources without initializing or repairing stores."""
import hashlib
import json
import os
import sqlite3
import shutil

import pytest

from chatcopilot.core.private_sqlite import json_text,private_directory
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.store import HarnessStore,read_source_metadata


def persist_source(root,task_id,source):
    store=HarnessStore(root)
    directory=private_directory(store.root/'jobs'/task_id)
    artifacts=private_directory(directory/'artifacts')
    raw=json_text(source).encode('utf-8')
    digest=hashlib.sha256(raw).hexdigest()
    file=artifacts/(digest+'.json')
    file.write_bytes(raw)
    file.chmod(0o600)
    reference={'path':'artifacts/'+digest+'.json','sha256':digest,'kind':'source','revision':1}
    payload={'task_id':task_id,'source':{'kind':'application'},'artifact_fields':{'source':reference}}
    with store.database.connect(write=True) as db:
        db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)',
            (task_id,'request-'+task_id,'match','context',None,'failed',json.dumps(payload),1,1))
    return store


def snapshot(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def test_existing_directory_never_creates_a_missing_path(tmp_path):
    path=tmp_path/'missing'/'child'
    with pytest.raises(FileNotFoundError):
        private_directory(path,create=False)
    assert not (tmp_path/'missing').exists()


@pytest.mark.skipif(os.name=='nt',reason='NTFS privacy uses DACLs, not chmod mode bits')
def test_existing_directory_never_repairs_permissions(tmp_path):
    path=tmp_path/'shared'
    path.mkdir(mode=0o755)
    path.chmod(0o775)
    with pytest.raises(ValueError):
        private_directory(path,create=False)
    assert path.stat().st_mode&0o777==0o775


def test_original_source_is_read_through_its_existing_hash_reference_without_writes(tmp_path):
    root=tmp_path/'harness'
    task='repair-'+'a'*32
    expected={'kind':'application','verification_runtime':'b'*64}
    persist_source(root,task,expected)
    before=snapshot(root)
    assert read_source_metadata(root,task)==expected
    assert snapshot(root)==before


@pytest.mark.parametrize('case',['absent-directory','absent-file','empty-file','corrupt-file','empty-schema','missing-task'])
def test_missing_or_damaged_store_is_not_initialized_or_repaired(tmp_path,case):
    root=tmp_path/'harness'
    if case!='absent-directory':
        private_directory(root)
    path=root/'harness.sqlite3'
    if case in {'empty-file','corrupt-file'}:
        path.write_bytes(b'' if case=='empty-file' else b'old damaged metadata')
        path.chmod(0o600)
    elif case=='empty-schema':
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE legacy(value TEXT)')
        path.chmod(0o600)
    elif case=='missing-task':
        HarnessStore(root)
    before=snapshot(tmp_path)
    with pytest.raises((OSError,ValueError,sqlite3.Error,HarnessError)):
        read_source_metadata(root,'repair-'+'a'*32)
    assert snapshot(tmp_path)==before
    assert root.exists()==(case!='absent-directory')


@pytest.mark.parametrize('include_shm',[False,True])
def test_unsettled_wal_history_is_not_opened_or_repaired(tmp_path,include_shm):
    # SQLite mode=ro can still create/update WAL shared-memory files. A detached
    # old WAL database must remain history until its owner settles the journal.
    root=private_directory(tmp_path/'history')
    source=tmp_path/'writer.sqlite3'
    writer=sqlite3.connect(source)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE tasks(task_id TEXT PRIMARY KEY,payload TEXT)')
        task='repair-'+'c'*32
        writer.execute('INSERT INTO tasks VALUES(?,?)',(task,json.dumps({'task_id':task,'source':{'kind':'application'}})))
        writer.commit()
        for suffix in ('','-wal','-shm') if include_shm else ('','-wal'):
            target=root/('harness.sqlite3'+suffix)
            shutil.copyfile(str(source)+suffix,target)
            target.chmod(0o600)
        before=snapshot(root)
        with pytest.raises(HarnessError):
            read_source_metadata(root,task)
        assert snapshot(root)==before
    finally:
        writer.close()
