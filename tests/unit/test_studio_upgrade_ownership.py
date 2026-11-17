"""A stopped service does not prove its shared development consumer released ownership."""
import sqlite3
import sys

import pytest

from tests.unit.test_studio_windows_entry import launcher as launcher,executable

fcntl=pytest.importorskip('fcntl',reason='WSL bootstrap ownership uses POSIX file locks')


def wal_library(tmp_path,state,mode):
    source=tmp_path/'wal-seed.sqlite'
    db=sqlite3.connect(source)
    try:
        db.execute('CREATE TABLE messages(state TEXT)')
        db.execute("INSERT INTO messages VALUES ('ready')")
        db.commit()
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute("UPDATE messages SET state='developing'")
        db.commit()
        if mode=='checkpointed':
            db.close()
        # The uncheckpointed main file still says ready. Ignoring its WAL would
        # hide unfinished work; no immutable-main-file fallback is safe here.
        suffixes=('',) if mode=='checkpointed' else ('','-wal','-shm') if mode=='existing-shm' else ('','-wal')
        for suffix in suffixes:
            target=state/('studio.sqlite'+suffix)
            target.write_bytes(source.with_name(source.name+suffix).read_bytes())
            target.chmod(0o600)
    finally:
        db.close()
    return {p.name:p.read_bytes() for p in state.glob('studio.sqlite*')}


@pytest.mark.parametrize('mode',['checkpointed','uncheckpointed','existing-shm'])
def test_upgrade_refuses_wal_before_sqlite_can_create_or_modify_sidecars(launcher,tmp_path,mode):
    state,source,_env,trace,run=launcher
    old='b'*64+'\n'
    (state/'framework-version').write_text(old)
    before=wal_library(tmp_path,state,mode)
    result=run()
    assert result.returncode!=0
    assert {p.name:p.read_bytes() for p in state.glob('studio.sqlite*')}==before
    assert 'WAL-mode' in result.stderr
    assert (state/'framework-version').read_text()==old
    assert not (source/'.studio-ready').exists()
    assert all(name not in trace.read_text() for name in ('prepare','npm','systemd-run'))


def test_prepared_same_version_wal_library_reopens_without_probing_storage(launcher,tmp_path):
    state,source,_env,_trace,run=launcher
    (state/'framework-version').write_text('a'*64+'\n')
    (source/'.studio-ready').touch()
    (source/'console/web/dist').mkdir(parents=True)
    (source/'console/web/dist/index.html').touch()
    before=wal_library(tmp_path,state,'uncheckpointed')
    result=run()
    assert result.returncode==0,result.stderr
    assert {p.name:p.read_bytes() for p in state.glob('studio.sqlite*')}==before


@pytest.mark.parametrize('same_version',[False,True])
def test_preparation_refuses_a_consumer_still_owning_the_project_library(launcher,same_version):
    state,source,_env,trace,run=launcher
    old_version=('a' if same_version else 'b')*64+'\n'
    (state/'framework-version').write_text(old_version)
    with sqlite3.connect(state/'studio.sqlite') as db:
        db.execute('CREATE TABLE messages(state TEXT)')
    with (state/'development-worker.lock').open('a') as stream:
        (state/'development-worker.lock').chmod(0o600)
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=run()
    assert result.returncode!=0
    assert 'development consumer' in result.stderr
    assert not (source/'.studio-ready').exists()
    assert (state/'framework-version').read_text()==old_version
    calls=trace.read_text()
    assert 'prepare' not in calls and 'npm' not in calls and 'systemd-run' not in calls


def test_prepared_same_version_reopen_does_not_take_consumer_ownership(launcher):
    state,source,_env,_trace,run=launcher
    (state/'framework-version').write_text('a'*64+'\n')
    (source/'.studio-ready').touch()
    (source/'console/web/dist').mkdir(parents=True)
    (source/'console/web/dist/index.html').touch()
    with (state/'development-worker.lock').open('a') as stream:
        (state/'development-worker.lock').chmod(0o600)
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=run()
    assert result.returncode==0,result.stderr


def test_consumer_lock_is_held_during_frontend_and_readiness_then_released(launcher,tmp_path):
    state,source,env,_trace,run=launcher
    helper=tmp_path/'check_ownership.py'
    helper.write_text('''import fcntl,os,sys
from pathlib import Path
with (Path(os.environ['AGENTSTRATA_STUDIO_HOME'])/'development-worker.lock').open('a') as stream:
    try:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
sys.exit('Preparation lost consumer ownership')
''')
    env.update(TEST_PYTHON=sys.executable,CONSUMER_CHECK=str(helper))
    for path in (tmp_path/'bin/npm',source/'.venv/bin/python'):
        original=path.read_text()
        path.write_text(original.replace('#!/bin/bash\n','#!/bin/bash\n"$TEST_PYTHON" "$CONSUMER_CHECK" || exit $?\n'))
    result=run()
    assert result.returncode==0,result.stderr
    with (state/'development-worker.lock').open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert (state/'framework-version').read_text()=='a'*64+'\n'


def test_failed_preparation_preserves_version_and_releases_ownership(launcher,tmp_path):
    state,_source,_env,_trace,run=launcher
    old='b'*64+'\n'
    (state/'framework-version').write_text(old)
    executable(tmp_path/'bin/npm','exit 35\n')
    result=run()
    assert result.returncode==35
    assert (state/'framework-version').read_text()==old
    with (state/'development-worker.lock').open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)


def test_same_version_missing_frontend_does_not_rebuild_over_a_recoverable_task(launcher):
    state,source,_env,trace,run=launcher
    (state/'framework-version').write_text('a'*64+'\n')
    (source/'.studio-ready').touch()
    with sqlite3.connect(state/'studio.sqlite') as db:
        db.execute('CREATE TABLE messages(state TEXT)')
        db.execute("INSERT INTO messages VALUES ('developing')")
    result=run()
    assert result.returncode!=0
    assert 'unfinished development/build tasks' in result.stderr
    assert 'npm' not in trace.read_text()
    assert (state/'framework-version').read_text()=='a'*64+'\n'


@pytest.mark.parametrize('status',['selecting','importing','unknown-import-state'])
def test_same_version_missing_frontend_preserves_active_or_unknown_imports(launcher,status):
    state,_source,_env,trace,run=launcher
    (state/'framework-version').write_text('a'*64+'\n')
    with sqlite3.connect(state/'studio.sqlite') as db:
        db.execute('CREATE TABLE messages(state TEXT)')
        db.execute('CREATE TABLE studio_imports(state TEXT)')
        db.execute('INSERT INTO studio_imports VALUES (?)',(status,))
    result=run()
    assert result.returncode!=0
    assert 'unfinished or interrupted Live2D imports' in result.stderr
    assert 'npm' not in trace.read_text()
    with sqlite3.connect(state/'studio.sqlite') as db:
        assert db.execute('SELECT state FROM studio_imports').fetchone()[0]==status
