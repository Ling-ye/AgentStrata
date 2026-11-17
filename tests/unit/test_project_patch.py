"""Original projects and exact patches stay inert, bounded and independently pinned."""
from dataclasses import replace
import gzip
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.project_patch import ProjectPatchLimits, exact_project_patch, project_source_mapping
from chatcopilot.core import project_materialize as writer
from chatcopilot.core.tar_inventory import git_source_tar_inventory,git_project_tar_inventory
from tests.unit.test_python_environment import private_directory as windows_private_directory
from tests.unit.test_python_plan import digest
from tests.unit.test_tar_inventory import archive, member, pax_record,pax,longname

def private_directory(path):
    import os
    if os.name=='nt':
        return windows_private_directory(path)
    path.mkdir(mode=0o700)
    return path

REV='a'*40
PATCH=b'''diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -1,2 +1,2 @@
 keep
-before
+after
'''


def original(name='backend',extra=None,patch=PATCH):
    repository='Open-LLM-VTuber' if name=='backend' else 'Open-LLM-VTuber-Web'
    root=repository+'-'+REV
    files={'main.py':b'keep\nbefore\n','LICENSE':b'Original attribution\n','setup.py':b'raise RuntimeError("never execute")\n'}
    files.update(extra or {})
    raw=gzip.compress(archive(member('pax_global_header',pax_record(b'comment',REV.encode()),kind=b'g'),
        *(member(root+'/'+path,data) for path,data in files.items())),mtime=0)
    pins={'expected_archive_sha256':digest(raw),'expected_revision':REV,'expected_patch_sha256':digest(patch),
        'source_root':root,'target_root':'runtime/apps/'+name,
        'allowed_cjk_paths':tuple(path for path in files if not path.isascii())}
    return raw,patch,pins


@pytest.fixture(autouse=True)
def no_external_or_extraction(monkeypatch):
    monkeypatch.setattr(subprocess,'Popen',lambda *_a,**_k:pytest.fail('project data operation executed a program'))
    monkeypatch.setattr(tarfile.TarFile,'extractall',lambda *_a,**_k:pytest.fail('project extraction API'))
    monkeypatch.setattr(tarfile.TarFile,'extract',lambda *_a,**_k:pytest.fail('project extraction API'))


def test_complete_original_bytes_and_patch_lineage_are_deterministic_and_inert():
    source,patch,pins=original(extra={'characters/中文.yaml':b'original character\n'})
    plan,contents=project_source_mapping(source,patch,**pins)
    assert contents['runtime/apps/backend/main.py']==b'keep\nafter\n'
    assert contents['runtime/apps/backend/LICENSE']==b'Original attribution\n'
    assert contents['runtime/apps/backend/characters/中文.yaml']==b'original character\n'
    changed=next(file for file in plan.files if file.path.endswith('/main.py'))
    assert changed.patch_sha256==digest(patch) and changed.source_sha256==digest(b'keep\nbefore\n')
    assert plan.changed_files==('main.py',) and len(plan.files)==4
    assert not plan.installation_accepted and not plan.frontend_built and not plan.target_programs_executed
    assert project_source_mapping(source,patch,**pins)==(plan,contents)
    with pytest.raises(InstallError):
        git_source_tar_inventory(source,expected_sha256=pins['expected_archive_sha256'],expected_revision=REV)


def test_exact_addition_has_no_original_member_and_keeps_unmodified_missing_final_newline():
    patch=b'diff --git a/new.py b/new.py\nnew file mode 100644\nindex 0000000..abcdef0\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+new\n'
    files,changed=exact_project_patch({'untouched':b'no final newline'},patch,expected_patch_sha256=digest(patch))
    assert changed==('new.py',) and files=={'untouched':b'no final newline','new.py':b'new\n'}


@pytest.mark.parametrize('old,new',[
    (b'a/main.py b/main.py',b'a/main.py b/other.py'),(b'a/main.py b/main.py',b'a/../main.py b/../main.py'),
    (b'@@ -1,2 +1,2 @@',b'@@ -2,2 +1,2 @@'),(b'@@ -1,2 +1,2 @@',b'@@ -1,2 +2,2 @@'),
    (b'@@ -1,2 +1,2 @@',b'@@ -1,3 +1,2 @@'),(b' keep\n',b' wrong\n'),
    (b'-before\n',b'-wrong\n'),(b'+after\n',b'+after\n\\ No newline at end of file\n'),
    (b'--- a/main.py\n',b'old mode 100644\nnew mode 100755\n--- a/main.py\n'),
    (b'--- a/main.py\n',b'Binary files differ\n--- a/main.py\n'),
    (b'+++ b/main.py\n',b'+++ /dev/null\n'),(b'@@ -1,2 +1,2 @@',b'@@ -0,0 +0,0 @@'),
    (b'--- a/main.py\n',b'index abcdef0..1234567 100755\n--- a/main.py\n'),
])
def test_unsupported_or_inexact_patch_cannot_create_a_lab(tmp_path,old,new):
    source,patch,pins=original(patch=PATCH.replace(old,new))
    lab=tmp_path/'absent'
    with pytest.raises(InstallError):
        writer.materialize_project_sources_isolated((writer.ProjectSourceInput(source,patch,**pins),),workspace=lab)
    assert not lab.exists()


@pytest.mark.parametrize('field',['expected_archive_sha256','expected_patch_sha256','expected_revision','source_root','recipe'])
def test_independent_pins_precede_any_storage(tmp_path,field):
    source,patch,pins=original()
    pins[field]='0'*40 if field=='expected_revision' else ('other-'+REV if field=='source_root' else '0'*64)
    with pytest.raises(InstallError):
        writer.materialize_project_sources_isolated((writer.ProjectSourceInput(source,patch,**pins),),workspace=tmp_path/'absent')
    assert not (tmp_path/'absent').exists()


@pytest.mark.parametrize('names',[
    (),('characters/别的.yaml',),('characters/中文.yaml','characters/中文.yaml'),
    ('characters/中é.yaml',),('characters/中\u0301.yaml',),('characters/中文.yaml','missing/另一个.yaml'),
])
def test_project_unicode_policy_is_exact_complete_and_finite(names):
    source,patch,pins=original(extra={'characters/中文.yaml':b'character'})
    pins['allowed_cjk_paths']=names
    with pytest.raises(InstallError):
        project_source_mapping(source,patch,**pins)


def test_cjk_case_alias_and_patch_generated_case_alias_are_rejected():
    source,patch,pins=original(extra={'characters/中文.yaml':b'a','characters/中文.YAML':b'b'})
    with pytest.raises(InstallError,match='collid'):
        project_source_mapping(source,patch,**pins)

    added=b'diff --git a/MAIN.py b/MAIN.py\nnew file mode 100644\n--- /dev/null\n+++ b/MAIN.py\n@@ -0,0 +1 @@\n+alias\n'
    source,patch,pins=original(patch=added)
    with pytest.raises(InstallError,match='collid'):
        project_source_mapping(source,patch,**pins)

@pytest.mark.parametrize('encoding',['ustar','pax','gnu-longname'])
def test_exact_project_cjk_names_match_both_tar_readers(encoding):
    root='Open-LLM-VTuber-'+REV
    name=root+'/characters/中文.yaml'
    rows=member(name,b'character')
    if encoding=='pax':
        rows=pax(pax_record(b'path',name.encode('utf-8')))+member(root+'/short',b'character')
    elif encoding=='gnu-longname':
        rows=longname(name.encode('utf-8')+b'\0')+member(root+'/short',b'character')
    patch=b'diff --git a/main.py b/main.py\nnew file mode 100644\n--- /dev/null\n+++ b/main.py\n@@ -0,0 +1 @@\n+main\n'
    raw=gzip.compress(archive(member('pax_global_header',pax_record(b'comment',REV.encode()),kind=b'g'),rows),mtime=0)
    plan,contents=project_source_mapping(raw,patch,expected_archive_sha256=digest(raw),expected_revision=REV,
        expected_patch_sha256=digest(patch),source_root=root,target_root='runtime/apps/backend',allowed_cjk_paths=('characters/中文.yaml',))
    assert contents['runtime/apps/backend/characters/中文.yaml']==b'character' and len(plan.files)==2


@pytest.mark.parametrize('name',[
    '目录/file.yaml','characters/中é.yaml','characters/中\u0301.yaml','characters/中\u200d文.yaml',
    'characters/中文.yaml:stream','characters/中文.yaml.','characters/COM1.中文','characters/中文../file',
])
def test_project_cjk_policy_keeps_windows_alias_and_encoding_rejections(name):
    source,patch,pins=original(extra={name:b'file'})
    with pytest.raises(InstallError):
        project_source_mapping(source,patch,**pins)


@pytest.mark.parametrize('allowed',[[],['anything'],('a',),([1],),None])
def test_project_archive_policy_rejects_untyped_or_ascii_exceptions(allowed):
    source,_patch,pins=original()
    with pytest.raises(InstallError):
        git_project_tar_inventory(source,expected_sha256=pins['expected_archive_sha256'],expected_revision=REV,allowed_cjk_paths=allowed)



@pytest.mark.parametrize('field',['max_patch_bytes','max_hunks','max_changed_files','max_files','max_total_bytes'])
def test_finite_project_budget_rejects_before_staging(field,tmp_path):
    source,patch,pins=original(extra={'other.py':b'keep\nbefore\n'})
    if field=='max_hunks':
        patch+=PATCH.replace(b'main.py',b'other.py')
        pins['expected_patch_sha256']=digest(patch)
    elif field=='max_changed_files':
        patch+=PATCH.replace(b'main.py',b'other.py')
        pins['expected_patch_sha256']=digest(patch)
    limits=replace(ProjectPatchLimits(),**{field:1})
    with pytest.raises(InstallError):
        writer.materialize_project_sources_isolated((writer.ProjectSourceInput(source,patch,**pins),),workspace=tmp_path/'absent',
            limits=writer.ProjectCopyLimits(planning=limits))
    assert not (tmp_path/'absent').exists()


def test_private_file_staging_and_independent_reread_reject_tampering(tmp_path):
    source,patch,pins=original(extra={'characters/中文.yaml':b'character'})
    inputs=(writer.ProjectSourceInput(source,patch,**pins),)
    lab=private_directory(tmp_path/'lab')
    evidence=writer.materialize_project_sources_isolated(inputs,workspace=lab)
    assert evidence.files==4 and not evidence.installation_accepted and not evidence.executable_prefix_created
    from pathlib import Path
    stage=Path(evidence.stage_root)
    assert writer.verify_project_sources_isolated(inputs,stage_root=stage)==evidence
    (stage/'runtime/apps/backend/main.py').write_bytes(b'tampered')
    with pytest.raises(writer.IsolatedProjectError):
        writer.verify_project_sources_isolated(inputs,stage_root=stage)
    assert stage.exists()


def test_space_headroom_and_existing_lab_are_preserved(tmp_path,monkeypatch):
    source,patch,pins=original()
    inputs=(writer.ProjectSourceInput(source,patch,**pins),)
    lab=private_directory(tmp_path/'lab')
    monkeypatch.setattr(writer.shutil,'disk_usage',lambda _path:SimpleNamespace(free=0))
    with pytest.raises(InstallError,match='headroom'):
        writer.materialize_project_sources_isolated(inputs,workspace=lab)
    assert not list(lab.iterdir())
    monkeypatch.undo()
    (lab/'user-file').write_bytes(b'keep')
    with pytest.raises(writer.IsolatedProjectError):
        writer.materialize_project_sources_isolated(inputs,workspace=lab)
    assert (lab/'user-file').read_bytes()==b'keep' and len(list(lab.iterdir()))==1


def test_partial_write_is_retained_and_cannot_verify_or_overwrite(tmp_path,monkeypatch):
    source,patch,pins=original()
    inputs=(writer.ProjectSourceInput(source,patch,**pins),)
    lab=private_directory(tmp_path/'lab')
    real=writer._write
    calls=[]
    def fail(*args):
        real(*args)
        calls.append(True)
        raise OSError('injected after first durable file')
    monkeypatch.setattr(writer,'_write',fail)
    with pytest.raises(writer.IsolatedProjectError) as error:
        writer.materialize_project_sources_isolated(inputs,workspace=lab)
    assert calls and error.value.stage_root and len(list(lab.iterdir()))==1
    from pathlib import Path
    with pytest.raises(writer.IsolatedProjectError):
        writer.verify_project_sources_isolated(inputs,stage_root=Path(error.value.stage_root))
    with pytest.raises(writer.IsolatedProjectError):
        writer.materialize_project_sources_isolated(inputs,workspace=lab)
