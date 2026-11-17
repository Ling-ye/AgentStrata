"""Read installed-schema history without creating or rewriting missing tasks.

This SQL matches workspace.py in the retained 0.1.0 framework archive
ad5d7d3319791b00247894a788e9d9d77df6a7fc1c91e9ffbff0a7b58a1d950a.
That archive is compatibility data, never authority to execute old code.
"""
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from studio import runtime
from studio.console_server import application
from studio.workspace import Studio

ROOT=Path(__file__).resolve().parents[2]
LEGACY_SCHEMA='''
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, bot TEXT NOT NULL, version TEXT NOT NULL, assets TEXT, created REAL NOT NULL);
CREATE TABLE messages(id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), text TEXT NOT NULL,
 state TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', goal TEXT, task TEXT, version TEXT, build TEXT, launched REAL, updated REAL NOT NULL);
CREATE INDEX conversation ON messages(project);
CREATE UNIQUE INDEX one_operation ON messages(project) WHERE state IN ('thinking','starting','developing','verifying','packaging');
'''


@pytest.fixture
def old_project(tmp_path):
    home=tmp_path/'old-studio'
    home.mkdir(mode=0o700)
    project,message=uuid4().hex,uuid4().hex
    db=home/'studio.sqlite'
    with sqlite3.connect(db) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute('PRAGMA user_version=1')
        connection.execute('INSERT INTO projects VALUES (?,?,?,?,?,?)',(project,'Existing desktop','app-'+project,'initial',None,1))
        connection.execute('INSERT INTO messages(id,project,text,state,reply,task,version,updated) VALUES (?,?,?,?,?,?,?,?)',
            (message,project,'Original request','failed','Original historical reply','repair-'+uuid4().hex,'initial',2))
    db.chmod(0o600)
    studio=Studio(home,ROOT,model_path=home/'models.json')
    work=studio.directory(project)
    (work/'initial').mkdir(mode=0o700)
    runtime.task_verification_runtime.cache_clear()
    yield studio,project,message,work
    runtime.task_verification_runtime.cache_clear()


def original_row(studio,message):
    with studio.db.connect() as db:
        return tuple(db.execute('SELECT * FROM messages WHERE id=?',(message,)).fetchone())


def test_missing_original_task_is_read_only_and_does_not_initialize_a_harness(old_project):
    studio,project,message,work=old_project
    before=original_row(studio,message)
    rows=studio.conversation(project)
    assert rows[0]['read_only'] and not rows[0]['retryable']
    assert original_row(studio,message)==before
    assert not (work/'harness').exists()


@pytest.mark.parametrize('state',['starting','developing','verifying','packaging'])
def test_unverifiable_active_history_is_visible_but_never_consumed_or_rewritten(old_project,state):
    studio,project,message,work=old_project
    studio.update(message,state,'Original active reply')
    before=original_row(studio,message)
    assert message not in studio.pending()
    studio.step(message)
    assert original_row(studio,message)==before
    view=studio.conversation(project)[0]
    assert view['state']==state and view['read_only'] and view['blocks_development']
    assert view['recovery_message'] and not (work/'harness').exists()


def test_unified_history_api_handles_missing_task_and_keeps_original_retry_read_only(old_project):
    studio,project,message,work=old_project
    before=original_row(studio,message)
    app=application(studio.state_dir,ROOT,'fixture-token')
    app.state.applications._discover=lambda:[]
    client=TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345))
    assert client.post('/api/login',json={'token':'fixture-token'}).status_code==200
    path='/api/applications/studio:'+project
    response=client.get(path+'/messages')
    assert response.status_code==200 and response.json()[0]['read_only']
    retried=client.post(path+'/messages/'+message+'/retry',json={})
    assert retried.status_code==400 and '只读' in retried.json()['detail']
    assert original_row(studio,message)==before and not (work/'harness').exists()
    new=uuid4().hex
    assert client.post(path+'/messages',json={'id':new,'text':'New explicit request'}).status_code==200
    assert original_row(studio,message)==before


def test_unverifiable_active_task_blocks_new_requests_for_only_its_original_project(old_project):
    studio,project,message,_work=old_project
    studio.update(message,'developing','Original active reply')
    with pytest.raises(sqlite3.IntegrityError):
        studio.submit(project,uuid4().hex,'Do not overlap an unknown old task')
    other=uuid4().hex
    studio.create(other)
    next_message=uuid4().hex
    studio.submit(other,next_message,'Unrelated project still works')
    assert studio.pending()==[next_message]


@pytest.mark.parametrize('change',['snapshot','task-database','corrupt-task-database'])
def test_explicit_retry_rechecks_frozen_bytes_after_normal_polling_cached_them(old_project,tmp_path,monkeypatch,change):
    import subprocess
    studio,project,message,work=old_project
    from tests.unit.test_studio_readonly_metadata import persist_source
    platform=tmp_path/'fixture-host'
    platform.mkdir()
    subprocess.run(['git','init','-q',str(platform)],check=True)
    module=platform/'src/chatcopilot/evals/rules.py'
    module.parent.mkdir(parents=True)
    module.write_text('VALUE=1\n')
    monkeypatch.setattr(runtime,'__file__',str(platform/'studio/runtime.py'))
    frozen,identity=runtime.verification_runtime(work)
    with studio.db.connect() as db:
        task=db.execute('SELECT task FROM messages WHERE id=?',(message,)).fetchone()[0]
    persist_source(work/'harness',task,{'kind':'application','verification_runtime':identity})
    try:
        assert studio.conversation(project)[0]['retryable']
        if change=='snapshot':
            (frozen/'src/chatcopilot/evals/rules.py').write_text('VALUE=2\n')
        elif change=='task-database':
            (work/'harness/harness.sqlite3').unlink()
        else:
            (work/'harness/harness.sqlite3').write_bytes(b'damaged original task database')
        before=original_row(studio,message)
        with pytest.raises(ValueError,match='只读'):
            studio.retry(project,message)
        assert original_row(studio,message)==before
        assert studio.conversation(project)[0]['read_only']
        if change=='task-database':
            assert not (work/'harness/harness.sqlite3').exists()
        if change=='corrupt-task-database':
            assert (work/'harness/harness.sqlite3').read_bytes()==b'damaged original task database'
    finally:
        runtime.verification_runtime.cache_clear()
