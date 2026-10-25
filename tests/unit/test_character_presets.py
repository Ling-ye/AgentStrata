"""Character persistence and transport contracts; synthetic models do not prove SDK loading."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import zipfile

import httpx
import pytest

from integrations.live2d_generator.agentstrata_live2d.character_repository import CharacterRepository
from integrations.live2d_generator.agentstrata_live2d.character_service import CharacterService
from integrations.live2d_generator.agentstrata_live2d.character_types import CATALOG_FORMAT, profile
from integrations.live2d_generator.agentstrata_live2d.github_presets import GitHubPresets
from integrations.live2d_generator.agentstrata_live2d.preset_files import digest, read_json
from integrations.live2d_generator.agentstrata_live2d.preset_service import PresetService, build_catalog, release_entry
from integrations.live2d_generator.agentstrata_live2d.repository import JobRepository, write_bytes, write_json
from integrations.live2d_generator.agentstrata_live2d.types import GenerationError, GenerationRequest
from tests.unit.test_live2d_generation import archive, archive_bundle, bundle, service_fixture, vtuber_target


def details(name="测试角色"):
    return profile({"name": name, "description": "原创测试素材", "identity": "你是测试角色，喜欢天文。",
                    "response_style": "简短、温和。", "author": "Example", "license": "CC0-1.0",
                    "source": "Synthetic test fixture, not an SDK-validated model"})


def library(root: Path):
    return CharacterService(CharacterRepository(root / "characters"), JobRepository(root / "jobs"))


def saved_model(tmp_path):
    service = library(tmp_path / "author")
    model = archive_bundle(tmp_path / "model.zip", bundle(tmp_path / "model"))
    saved = service.import_model(model, details())
    return service, saved


def preset_fixture(tmp_path):
    service, saved = saved_model(tmp_path)
    output = tmp_path / "preset.zip"
    service.export(saved["id"], saved["version"], output)
    return service, saved, output


def test_same_names_are_distinct_and_history_is_immutable(tmp_path):
    service, first = saved_model(tmp_path)
    other = service.create(details())
    assert first["id"] != other["id"]
    service.save_draft(first["id"], details("新版角色"))
    second = service.save_version(first["id"], 1)
    assert second["version"] == 2
    assert service.revision(first["id"], 1)["profile"]["name"] == "测试角色"
    assert service.revision(first["id"], 2)["profile"]["name"] == "新版角色"
    assert service.get(first["id"])["versions"] == [1, 2]


def test_completed_job_is_copied_once_and_export_excludes_private_sources(tmp_path):
    generator, _ = service_fixture(tmp_path)
    job = generator.submit(GenerationRequest("测试角色", "private creative prompt"), None)
    generator.run(job)
    root = generator.repository.path(job)
    (root / "config.json").write_text('private credential canary')
    (root / "worker.log").write_text('private log canary')
    service = CharacterService(CharacterRepository(tmp_path / "characters"), generator.repository)
    saved = service.collect_job(job)
    assert service.collect_job(job)["version"] == saved["version"]
    assert len(service.list()) == 1
    assert generator.repository.read(job)["character_id"] == saved["id"]
    service.save_draft(saved["id"], details())
    second = service.save_version(saved["id"])
    shutil.rmtree(root)
    record = service.revision(saved["id"], second["version"])
    assert "sources/request.json" in record["files"]
    assert not any("config.json" in name or "worker.log" in name for name in record["files"])
    output = tmp_path / "preset.zip"
    service.export(saved["id"], second["version"], output)
    with zipfile.ZipFile(output) as packed:
        assert set(packed.namelist()) == {"preset.json", "profile.json", "avatar.zip", "preview.png"}
        assert b"private creative prompt" not in b"".join(packed.read(name) for name in packed.namelist())


def test_export_is_reproducible_and_does_not_overwrite(tmp_path):
    service, saved, first = preset_fixture(tmp_path)
    second = tmp_path / "again.zip"
    service.export(saved["id"], saved["version"], second)
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(GenerationError, match="覆盖"):
        service.export(saved["id"], saved["version"], first)


def test_import_is_idempotent_readonly_and_fork_is_independent(tmp_path):
    _, saved, source = preset_fixture(tmp_path)
    receiver = library(tmp_path / "receiver")
    imported = receiver.import_preset(source)
    assert receiver.import_preset(source) == imported
    with pytest.raises(GenerationError, match="副本"):
        receiver.save_draft(saved["id"], details("修改"))
    forked = receiver.fork(saved["id"], 1)
    receiver.save_draft(forked["id"], details("本地修改"))
    receiver.save_version(forked["id"])
    assert receiver.get(saved["id"])["versions"] == [1]
    assert receiver.get(forked["id"])["versions"] == [1, 2]
    assert receiver.get(forked["id"])["origin"]["derived_from"] == {"id": saved["id"], "version": 1}


def test_downloaded_version_cannot_change_and_local_id_cannot_be_replaced(tmp_path):
    service, saved, source = preset_fixture(tmp_path)
    with pytest.raises(GenerationError, match="本地作品冲突"):
        service.import_preset(source)
    receiver = library(tmp_path / "receiver")
    receiver.import_preset(source)
    with zipfile.ZipFile(source) as packed:
        entries = {name: packed.read(name) for name in packed.namelist()}
    entries["preset.json"] += b"\n"
    changed = archive(tmp_path / "changed.zip", entries)
    with pytest.raises(GenerationError, match="内容发生变化"):
        receiver.import_preset(changed)
    assert receiver.get(saved["id"])["versions"] == [1]


def test_backup_restores_drafts_sources_and_versions_and_is_idempotent(tmp_path):
    service, saved = saved_model(tmp_path)
    draft = service.create(details("只有草稿"))
    service.save_version(saved["id"])
    destination = tmp_path / "backup"
    service.backup(destination)
    receiver = library(tmp_path / "receiver")
    result = receiver.restore(destination)
    assert set(result["restored"]) == {saved["id"], draft["id"]}
    assert receiver.get(draft["id"])["versions"] == []
    assert receiver.get(saved["id"])["versions"] == [1, 2]
    assert receiver.restore(destination)["restored"] == []
    assert service.revision(saved["id"], 2) == receiver.revision(saved["id"], 2)


def test_restore_prechecks_all_conflicts_before_installing_any_character(tmp_path):
    service, saved = saved_model(tmp_path)
    receiver = library(tmp_path / "receiver")
    baseline = tmp_path / "baseline"
    service.backup(baseline)
    receiver.restore(baseline)
    added = service.create(details("新增"))
    service.save_draft(saved["id"], details("冲突"))
    updated = tmp_path / "updated"
    service.backup(updated)
    with pytest.raises(GenerationError, match="冲突"):
        receiver.restore(updated)
    assert not receiver.repository.path(added["id"]).exists()
    assert receiver.get(saved["id"])["draft"]["name"] == "测试角色"


def test_corrupt_backup_cannot_mutate_library(tmp_path):
    service, saved = saved_model(tmp_path)
    backup = tmp_path / "backup"
    service.backup(backup)
    (backup / saved["id"] / "draft.json").write_text("corrupt")
    receiver = library(tmp_path / "receiver")
    with pytest.raises(GenerationError, match="摘要"):
        receiver.restore(backup)
    assert receiver.list() == []


def test_backup_rejects_links_and_target_inside_library(tmp_path):
    service, saved = saved_model(tmp_path)
    with pytest.raises(GenerationError, match="之外"):
        service.backup(service.repository.root / "nested")
    (service.repository.path(saved["id"]) / "secret-link").symlink_to(tmp_path / "model.zip")
    with pytest.raises(GenerationError, match="符号链接"):
        service.backup(tmp_path / "backup")


@pytest.mark.parametrize("bad", [
    {"voice": {"provider": "other", "voice": "voice"}},
    {"voice": {"provider": "edge_tts", "voice": "voice", "endpoint": "https://example.org"}},
    {"identity": 123}, {"tools": ["shell"]}, {"name": ""},
])
def test_profile_rejects_executable_or_invalid_configuration(bad):
    with pytest.raises(GenerationError):
        profile(details() | bad)


@pytest.mark.parametrize("mutate", ["extra", "hash", "traversal", "inner"])
def test_invalid_preset_never_creates_a_character(tmp_path, mutate):
    _, _, source = preset_fixture(tmp_path)
    with zipfile.ZipFile(source) as packed:
        entries = {name: packed.read(name) for name in packed.namelist()}
    if mutate == "extra":
        entries["private.txt"] = b"unlisted"
    elif mutate == "hash":
        entries["profile.json"] += b"\n"
    elif mutate == "traversal":
        entries["../outside.json"] = b"{}"
    else:
        entries["avatar.zip"] = b"not a zip"
        doc = json.loads(entries["preset.json"])
        doc["files"]["avatar.zip"] = {"sha256": hashlib.sha256(entries["avatar.zip"]).hexdigest(), "size": 9}
        entries["preset.json"] = json.dumps(doc).encode()
    receiver = library(tmp_path / "receiver")
    with pytest.raises(GenerationError):
        receiver.import_preset(archive(tmp_path / "bad.zip", entries))
    assert receiver.list() == []
    assert not (tmp_path / "outside.json").exists()


def character_target(root, monkeypatch):
    target = vtuber_target(root, monkeypatch)
    write_bytes(target / "config/identity.md", b"original identity")
    write_bytes(target / "config/response-style.md", b"original style")
    write_json(target / "config/bot.yaml", {"prompts": {"schema_version": 2, "identity": "identity.md",
                                                        "response_style": "response-style.md"}, "llm": "keep"})
    return target


def test_apply_model_persona_voice_atomically_and_switch_back_to_existing_model(tmp_path, monkeypatch):
    import yaml
    from integrations.live2d_generator.agentstrata_live2d.install import CHARACTER_CONFIG_FILES
    service, saved = saved_model(tmp_path)
    target = character_target(tmp_path / "client", monkeypatch)
    original = {name: (target / name).read_bytes() for name in CHARACTER_CONFIG_FILES}
    bot = (target / "config/bot.yaml").read_bytes()
    applied = service.apply(saved["id"], 1, target)
    assert (target / "config/identity.md").read_text() == details()["identity"]
    tts = yaml.safe_load((target / "config/upstream.yaml").read_text())["character_config"]["tts_config"]
    assert tts == {"tts_model": "edge_tts", "edge_tts": {"voice": "zh-CN-XiaoxiaoNeural"}}
    service.restore_application(target, applied["backup"])
    assert original == {name: (target / name).read_bytes() for name in CHARACTER_CONFIG_FILES}
    assert service.apply(saved["id"], 1, target)["model"] == applied["model"]
    assert (target / "config/bot.yaml").read_bytes() == bot
    assert not (target / "data/workspace").exists()


@pytest.mark.parametrize("style", ["", " \n"])
def test_optional_empty_style_applies_a_nonempty_bot_layer_without_editing_the_version(tmp_path, monkeypatch, style):
    service, saved = saved_model(tmp_path)
    service.save_draft(saved["id"], details() | {"response_style": style})
    service.save_version(saved["id"], 1)
    version = service.repository.path(saved["id"]) / "versions/2/profile.json"
    original_version = version.read_bytes()
    target = character_target(tmp_path / "client", monkeypatch)
    applied = service.apply(saved["id"], 2, target)
    assert (target / "config/response-style.md").read_text().strip()
    assert (target / "config/response-style.md").read_bytes() != b"original style"
    assert version.read_bytes() == original_version
    service.restore_application(target, applied["backup"])
    assert (target / "config/response-style.md").read_bytes() == b"original style"


def test_late_persona_write_failure_restores_all_configuration(tmp_path, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d import install
    service, saved = saved_model(tmp_path)
    target = character_target(tmp_path / "client", monkeypatch)
    original = {name: (target / name).read_bytes() for name in install.CHARACTER_CONFIG_FILES}
    write = install.write_bytes
    failed = False
    def fail_once(path, payload):
        nonlocal failed
        if path == target / "config/response-style.md" and not failed:
            failed = True
            raise OSError("simulated disk error")
        return write(path, payload)
    monkeypatch.setattr(install, "write_bytes", fail_once)
    with pytest.raises(OSError):
        service.apply(saved["id"], 1, target)
    assert original == {name: (target / name).read_bytes() for name in install.CHARACTER_CONFIG_FILES}
    assert not (target / "config/pending-model-import.json").exists()


def test_restore_does_not_overwrite_user_persona_edits(tmp_path, monkeypatch):
    service, saved = saved_model(tmp_path)
    target = character_target(tmp_path / "client", monkeypatch)
    applied = service.apply(saved["id"], 1, target)
    write_bytes(target / "config/identity.md", b"edited later")
    with pytest.raises(GenerationError, match="后续修改"):
        service.restore_application(target, applied["backup"])
    assert (target / "config/identity.md").read_bytes() == b"edited later"


def test_custom_bot_prompt_binding_is_not_silently_overwritten(tmp_path, monkeypatch):
    service, saved = saved_model(tmp_path)
    target = character_target(tmp_path / "client", monkeypatch)
    write_json(target / "config/bot.yaml", {"prompts": {"schema_version": 2, "identity": "custom.md"}})
    with pytest.raises(GenerationError, match="绑定"):
        service.apply(saved["id"], 1, target)
    assert (target / "config/identity.md").read_bytes() == b"original identity"


def test_github_redirect_strips_auth_and_signed_urls_do_not_enter_logs(tmp_path, caplog):
    payload, requests = b"fixture", []
    secret = "fixture-credential-canary"
    signed = "signed-download-canary"
    def handler(request):
        requests.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://" + "release-assets." + "githubusercontent.com/example" + "?" + "sig=" + signed})
        return httpx.Response(200, content=payload)
    source = GitHubPresets("example/characters", secret, transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG):
        source.download(12, tmp_path / "asset.zip", size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    assert requests[0].headers["authorization"] == "Bearer " + secret
    assert "authorization" not in requests[1].headers
    assert secret not in caplog.text and signed not in caplog.text
    assert (tmp_path / "asset.zip").read_bytes() == payload


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_private_access_errors_are_redacted_and_leave_no_artifact(tmp_path, status):
    source = GitHubPresets("example/characters", "fixture", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text="sensitive response body")))
    with pytest.raises(GenerationError) as error:
        source.download(1, tmp_path / "asset.zip", size=1, sha256="a" * 64)
    assert "sensitive" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_missing_token_does_not_make_network_request(tmp_path):
    def handler(request):
        pytest.fail("No network without credentials")
    source = GitHubPresets("example/characters", "", transport=httpx.MockTransport(handler))
    with pytest.raises(GenerationError, match="TOKEN"):
        source.catalog()


@pytest.mark.parametrize("kind", ["hash", "oversize", "truncated", "redirect"])
def test_download_failure_is_atomic(tmp_path, kind):
    def handler(request):
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "https://example.org/asset"})
        return httpx.Response(200, content=b"xx" if kind == "oversize" else b"x")
    source = GitHubPresets("example/characters", "fixture", transport=httpx.MockTransport(handler))
    with pytest.raises(GenerationError):
        source.download(1, tmp_path / "asset.zip", size=2 if kind == "truncated" else 1, sha256="a" * 64)
    assert list(tmp_path.iterdir()) == []


def test_catalog_download_cache_and_release_helpers(tmp_path):
    author, saved = saved_model(tmp_path)
    release = tmp_path / "release"
    author.release_files(saved["id"], 1, release)
    entry = release_entry(release, 10, 11, tmp_path / "entry.json")
    build_catalog([tmp_path / "entry.json"], tmp_path / "catalog.json")
    doc = read_json(tmp_path / "catalog.json")
    assert doc == {"format": CATALOG_FORMAT, "entries": [entry]}
    requested = []
    def handler(request):
        requested.append(request)
        if "/contents/" in request.url.path:
            return httpx.Response(200, json=doc)
        path = release / ("preset.zip" if request.url.path.endswith("/10") else "preview.png")
        return httpx.Response(200, content=path.read_bytes())
    receiver = library(tmp_path / "receiver")
    presets = PresetService(receiver, tmp_path / "cache", GitHubPresets("example/characters", "fixture",
        transport=httpx.MockTransport(handler)))
    assert not presets.list()["cached"]
    presets.refresh()
    presets.download(saved["id"], 1)
    assert digest(presets.preview(saved["id"], 1)) == entry["preview_sha256"]
    count = len(requested)
    presets.source = None
    assert presets.download(saved["id"], 1)["id"] == saved["id"]
    assert presets.preview(saved["id"], 1).exists()
    assert len(requested) == count


def test_catalog_identity_mismatch_is_not_imported(tmp_path):
    _, _, source = preset_fixture(tmp_path)
    receiver = library(tmp_path / "receiver")
    with pytest.raises(GenerationError, match="目录不一致"):
        receiver.import_preset(source, expected=details() | {"id": "f" * 32, "version": 1})
    assert receiver.list() == []


def test_worker_collects_completed_model_into_its_character(tmp_path, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d import runtime
    generator, _ = service_fixture(tmp_path)
    characters = CharacterService(CharacterRepository(tmp_path / "characters"), generator.repository)
    role = characters.create(details())
    job = generator.submit(GenerationRequest("测试角色", "prompt"), None)
    state = generator.repository.read(job) | {"character_id": role["id"]}
    generator.repository.save(job, state)
    write_json(generator.repository.path(job) / "config.json", {})
    write_json(generator.repository.path(job) / "character-profile.json", details())
    monkeypatch.setattr(runtime, "service", lambda *args: generator)
    assert runtime.worker(generator.repository.root, job) == 0
    assert generator.repository.read(job)["character_version"] == 1
    assert characters.get(role["id"])["versions"] == [1]


def test_character_http_auth_input_validation_and_roundtrip(tmp_path):
    from fastapi.testclient import TestClient
    from integrations.live2d_generator.agentstrata_live2d.config import Config
    from integrations.live2d_generator.agentstrata_live2d.runtime import JobRuntime
    from integrations.live2d_generator.agentstrata_live2d.web import create_app
    runtime = JobRuntime(tmp_path / "jobs", Config())
    with TestClient(create_app(runtime), base_url="http://127.0.0.1:12394") as client:
        assert client.get("/api/characters").status_code == 403
        assert client.post("/api/presets/refresh").status_code == 403
        assert client.post("/api/characters", content=b"invalid").status_code == 403
        token = re.search(r'name="generator-token" content="([^"]+)"', client.get("/").text)[1]
        headers = {"x-generator-token": token}
        result = client.post("/api/characters", json={"profile": details()}, headers=headers)
        assert result.status_code == 200
        identifier = result.json()["id"]
        assert client.get(f"/api/characters/{identifier}", headers=headers).json()["draft"] == details()
        assert client.post(f"/api/characters/{identifier}/draft", headers=headers,
                           json={"profile": details("改名")}).status_code == 200
        assert client.post("/api/characters", headers=headers, json={"profile": details(), "path": "bad"}).status_code == 400
        assert client.post("/api/presets/refresh", headers=headers).status_code == 400
        assert client.post("/api/characters/import-preset", headers=headers, json={"input": None}).status_code == 400


def test_cli_backup_and_restore(tmp_path, capsys):
    from integrations.live2d_generator.agentstrata_live2d.cli import main
    service, saved = saved_model(tmp_path)
    backup = tmp_path / "backup"
    assert main(["characters", "backup", "--root", str(service.jobs.root), "--destination", str(backup)]) == 0
    receiver = library(tmp_path / "receiver")
    assert main(["characters", "restore", "--root", str(receiver.jobs.root), "--source", str(backup)]) == 0
    assert receiver.get(saved["id"])["versions"] == [1]
    assert '"restored"' in capsys.readouterr().out


def test_job_receipt_failure_can_be_recovered_without_duplicate_character(tmp_path, monkeypatch):
    generator, _ = service_fixture(tmp_path)
    job = generator.submit(GenerationRequest("role", "prompt"), None)
    generator.run(job)
    service = CharacterService(CharacterRepository(tmp_path / "characters"), generator.repository)
    save = generator.repository.save
    monkeypatch.setattr(generator.repository, "save", lambda *args: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError):
        service.collect_job(job)
    assert len(service.list()) == 1
    monkeypatch.setattr(generator.repository, "save", save)
    saved = service.collect_job(job)
    assert len(service.list()) == 1 and saved["version"] == 1


def test_mismatched_catalog_preview_is_not_imported(tmp_path):
    author, saved = saved_model(tmp_path)
    author.release_files(saved["id"], 1, tmp_path / "release")
    entry = release_entry(tmp_path / "release", 1, 2, tmp_path / "entry.json")
    entry["preview_sha256"] = "a" * 64
    receiver = library(tmp_path / "receiver")
    with pytest.raises(GenerationError, match="目录不一致"):
        receiver.import_preset(tmp_path / "release/preset.zip", expected=entry)
    assert receiver.list() == []


def test_invalid_character_identity_is_rejected_before_client_mutation(tmp_path, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d.install import install_character
    target = character_target(tmp_path / "client", monkeypatch)
    with pytest.raises(GenerationError, match="角色 ID"):
        install_character(tmp_path / "unused.zip", details(), target, identifier="invalid", version=1)
    assert (target / "config/identity.md").read_bytes() == b"original identity"


def test_failed_generation_inputs_survive_job_cleanup_and_library_backup(tmp_path):
    generator, _ = service_fixture(tmp_path, failure=RuntimeError("provider unavailable"))
    source = tmp_path / "reference.png"
    from tests.unit.test_live2d_generation import png
    png(source)
    job = generator.submit(GenerationRequest("role", "private draft", redraw=True), source)
    service = CharacterService(CharacterRepository(tmp_path / "characters"), generator.repository)
    role = service.create(details())
    service.preserve_job_input(job, role["id"])
    assert generator.run(job)["status"] == "failed"
    shutil.rmtree(generator.repository.path(job))
    backup = tmp_path / "backup"
    service.backup(backup)
    receiver = library(tmp_path / "receiver")
    receiver.restore(backup)
    assert receiver.get(role["id"])["draft_jobs"] == [job]
    restored = receiver.repository.path(role["id"]) / "draft-sources" / job
    assert (restored / "input.png").read_bytes() == source.read_bytes()
    assert read_json(restored / "request.json")["prompt"] == "private draft"


def test_runtime_does_not_start_provider_if_original_input_cannot_be_saved(tmp_path, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d.runtime import JobRuntime
    from integrations.live2d_generator.agentstrata_live2d.config import Config
    runtime = JobRuntime(tmp_path / "jobs", Config())
    source = archive_bundle(tmp_path / "layers.zip", bundle(tmp_path / "layers"))
    monkeypatch.setattr(runtime.characters, "preserve_job_input",
                        lambda *args: (_ for _ in ()).throw(OSError("full disk")))
    with runtime:
        with pytest.raises(GenerationError, match="未启动生成"):
            runtime.start(GenerationRequest("role"), source)
        assert runtime._process is None
        assert runtime.list()[0]["status"] == "failed"
