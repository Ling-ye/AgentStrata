"""Isolated new/legacy entry acceptance with real APIs, storage and ownership locks.

Development replies and installer bytes are controlled fixtures, never real model
development, independent acceptance, a Windows installation or a public release.
"""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from studio.console_server import application as unified_application
from studio.server import application as legacy_application
from studio.setup import Guide
from studio.workspace import Studio
from console.backend import app as console_app

ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_model_or_system_services(monkeypatch):
    monkeypatch.setattr(Guide,'start',lambda _self:None)
    monkeypatch.setattr(Guide,'stop',lambda _self:None)
    monkeypatch.setattr(console_app,'create_controller',lambda:None)


def browser(app):
    return TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345))


def login(client,token):
    assert client.post('/api/login',json={'token':token}).status_code==200


def test_parallel_entries_share_one_consumer_and_handoff_without_recreating_requests(tmp_path,monkeypatch):
    home=tmp_path/'studio'
    processed=[]
    condition=threading.Condition()
    def step(studio,message):
        studio.update(message,'discuss','Controlled discussion result')
        with condition:
            processed.append(message)
            condition.notify_all()
    monkeypatch.setattr(Studio,'step',step)
    legacy=legacy_application(home,ROOT,'legacy-token')
    unified=unified_application(home,ROOT,'unified-token')
    unified.state.applications._discover=lambda:[]
    with ExitStack() as old_entry,ExitStack() as new_entry:
        old=old_entry.enter_context(browser(legacy))
        login(old,'legacy-token')
        project=uuid4().hex
        assert old.post('/api/projects',json={'id':project}).status_code==200
        new=new_entry.enter_context(browser(unified))
        login(new,'unified-token')
        ident='studio:'+project
        first=uuid4().hex
        body={'id':first,'text':'Continue this original desktop project'}
        assert new.post('/api/applications/'+ident+'/messages',json=body).status_code==200
        with condition:
            assert condition.wait_for(lambda:first in processed,timeout=5)
        assert old.post('/api/projects/'+project+'/messages',json=body).status_code==200
        assert new.post('/api/applications/'+ident+'/messages',json=body).status_code==200
        assert processed==[first]
        assert old.get('/api/projects/'+project+'/messages').json()==new.get('/api/applications/'+ident+'/messages').json()
        # Close the original server while the unified entry stays alive. The
        # shared lock must transfer, and persisted history must remain in place.
        old_entry.close()
        second=uuid4().hex
        assert new.post('/api/applications/'+ident+'/messages',json={'id':second,'text':'Next request'}).status_code==200
        with condition:
            assert condition.wait_for(lambda:second in processed,timeout=5)
        assert processed==[first,second]
        rows=new.get('/api/applications/'+ident+'/messages').json()
        assert [row['id'] for row in rows]==[first,second]
        assert all(row['state']=='discuss' for row in rows)
        assert unified.state.applications.state_dir==legacy.state.studio.state_dir==home
        assert unified.state.applications.model_path==unified.state.llm.store.path==home/'models.json'


def test_legacy_history_and_verified_download_survive_unified_entry_restart(tmp_path):
    home=tmp_path/'studio'
    old=legacy_application(home,ROOT,'legacy-token',run_worker=False)
    project,message,other=uuid4().hex,uuid4().hex,uuid4().hex
    with browser(old) as client:
        login(client,'legacy-token')
        for ident in (project,other):
            assert client.post('/api/projects',json={'id':ident}).status_code==200
        assert client.post('/api/projects/'+project+'/messages',json={'id':message,'text':'Historical desktop request'}).status_code==200
        old.state.studio.update(message,'ready','Historical result',build=message)
    directory=old.state.studio.directory(project)/'builds'/message
    directory.mkdir(parents=True)
    content=b'inert fixture bytes; never a real installer'
    (directory/'application.exe').write_bytes(content)
    (directory/'build-receipt.json').write_text(json.dumps({'installer_sha256':hashlib.sha256(content).hexdigest()}))
    preserved=home/'user-note.txt'
    preserved.write_bytes(b'keep original user data')
    for _ in range(2):
        app=unified_application(home,ROOT,'new-token')
        app.state.applications._discover=lambda:[]
        with browser(app) as client:
            login(client,'new-token')
            catalog=client.get('/api/applications').json()['applications']
            assert {row['id'] for row in catalog}=={'studio:'+project,'studio:'+other}
            rows=client.get('/api/applications/studio:'+project+'/messages').json()
            assert rows[0]['id']==message and rows[0]['state']=='ready'
            response=client.get('/api/applications/studio:'+project+'/builds/'+message)
            assert response.status_code==200 and response.content==content
            assert 'attachment' in response.headers['content-disposition']
            assert client.get('/api/applications/studio:'+other+'/builds/'+message).status_code==404
            assert client.post('/api/applications/studio:'+project+'/chat',json={'id':uuid4().hex,'text':'business input'}).status_code==409
            assert len(client.get('/api/applications/studio:'+project+'/messages').json())==1
        assert preserved.read_bytes()==b'keep original user data'
        assert (directory/'application.exe').read_bytes()==content
