"""Character tool, voice source and immutable voice version regression tests."""
from __future__ import annotations

from pathlib import Path
import wave

import pytest

from integrations.live2d_generator.agentstrata_live2d import voice_jobs, voice_sources, voice_worker
from integrations.live2d_generator.agentstrata_live2d.config import Config
from integrations.live2d_generator.agentstrata_live2d.preset_files import digest
from integrations.live2d_generator.agentstrata_live2d.runtime import JobRuntime
from integrations.live2d_generator.agentstrata_live2d.types import GenerationError
from integrations.live2d_generator.agentstrata_live2d.voice_assets import FILES, publish_voice, resolve_voice
from tests.unit.test_character_presets import library, saved_model


def _fixture_voice(path: Path) -> Path:
    path.mkdir()
    for name in FILES:
        target = path / name
        if name.endswith(".wav"):
            with wave.open(str(target), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\xfa\x07" * 7)
        else:
            target.write_bytes(b"not real weights or synthesis")
    return path


def test_voice_assets_follow_backup_and_reused_prompt_versions(tmp_path):
    characters, first = saved_model(tmp_path)
    voice = publish_voice(characters.repository.root, _fixture_voice(tmp_path / "voice"))
    role = first["id"]
    profile = characters.get(role)["draft"] | {"voice": {"provider": "gpt_sovits", "voice": voice}}
    characters.save_draft(role, profile)
    trained = characters.save_version(role, first["version"])
    characters.save_draft(role, profile | {"identity": "New identity"})
    updated = characters.save_version(role, trained["version"])
    assert characters.revision(role, updated["version"])["profile"]["voice"] == profile["voice"]
    assert len(list((characters.repository.root / ".voices").iterdir())) == 1
    backup = tmp_path / "backup"
    characters.backup(backup, role)
    receiver = library(tmp_path / "second-machine")
    assert receiver.restore(backup)["restored"] == [role]
    assert receiver.restore(backup)["unchanged"] == [role]
    assert receiver.voice_preview(role, updated["version"], "preview-2.wav").is_file()
    root = resolve_voice(receiver.repository.root, voice)
    (root / "gpt.safetensors").write_bytes(b"altered fixture")
    with pytest.raises(GenerationError, match="清单"):
        receiver.revision(role, updated["version"])


def _source(**extra):
    return {"artist": "Speaker", "title": "Recording", "kind": "speech", "language": "zh"} | extra


def test_audio_input_requires_evidence_and_keeps_public_candidates(tmp_path):
    media = tmp_path / "voice.wav"
    media.write_bytes(b"fixture")
    with pytest.raises(GenerationError, match="依据"):
        voice_sources.upload_audio(tmp_path, media, _source(), "")
    registered = voice_sources.upload_audio(tmp_path, media,
        _source(page="https://example.com/watch?v=published-video"), "Personal recording")
    material = tmp_path / "material"
    material.mkdir()
    with pytest.raises(GenerationError) as unavailable:
        voice_sources.materialize(tmp_path, [_source(page="https://example.com/watch?v=published-video")], material)
    assert unavailable.value.code == "voice_input_required"
    result = voice_sources.materialize(tmp_path, [registered], material)
    assert result[0]["sha256"] == digest(media)
    assert result[0]["page"] == "https://example.com/watch?v=published-video"
    with pytest.raises(GenerationError, match="匹配"):
        voice_sources.materialize(tmp_path, [registered | {"artist": "Someone else"}], material)


def test_publisher_page_can_link_its_licensed_recording_on_a_cdn(monkeypatch, tmp_path):
    page = "https://example.com/recording"
    audio = "https://media.example.net/recording.wav"
    license_url = "https://creativecommons.org/licenses/by/4.0/"

    def public_file(url, target, **_):
        content = (f'<a rel="license" href="{license_url}">License</a><audio src="{audio}"></audio>'.encode()
                   if url == page else b"example recording")
        target.write_bytes(content)

    monkeypatch.setattr(voice_sources, "_public_file", public_file)
    (record,) = voice_sources.materialize(tmp_path, [_source(page=page, audio=audio, license=license_url)], tmp_path)
    assert (record["page"], record["audio"], record["permission"]) == (page, audio, license_url)


def test_owner_job_retries_return_original_without_second_worker(monkeypatch, tmp_path):
    runtime = JobRuntime(tmp_path / "jobs", Config.parse(
        {"image_base_url": "https://example.com/v1", "image_model": "image",
         "decompose_url": "https://example.com/layers"}))
    launched = []
    class Worker:
        def poll(self):
            return None
    monkeypatch.setattr(runtime, "_spawn", lambda command, _: launched.append(command) or Worker())
    request_id = "0" * 32
    args = {"name": "角色", "identity": "向你问好。", "response_style": "温和。", "artwork": "完整立绘。"}
    original = voice_jobs.generate(runtime, **args, request_id=request_id)
    assert voice_jobs.generate(runtime, **args, request_id=request_id) == original
    assert original["voice_job"]
    assert len(runtime.characters.list()) == len(launched) == 1
    with pytest.raises(GenerationError, match="请求编号"):
        voice_jobs.generate(runtime, **(args | {"name": "Other"}), request_id=request_id)


def test_sovits_hparams_export_keeps_plain_nested_config():
    class HParams:
        def __init__(self, **values):
            self.values = values

        def items(self):
            return self.values.items()

    config = HParams(model=HParams(version="v2", kernels=[3, 5]), data=HParams(rate=32000))
    assert voice_worker._plain_config(config) == {
        "model": {"version": "v2", "kernels": [3, 5]}, "data": {"rate": 32000},
    }


def test_stage_resume_reuses_only_verified_files(tmp_path):
    first, next_run = tmp_path / "first", tmp_path / "next"
    first.mkdir()
    next_run.mkdir()
    payload = first / "stage.dat"
    payload.write_bytes(b"fixture")
    voice_worker.checkpoint(first, None, "features", lambda: [payload])
    voice_worker.checkpoint(next_run, first, "features", lambda: pytest.fail("repeated stage"))
    assert digest(next_run / payload.name) == digest(payload)
    payload.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="invalid prior stage receipt"):
        voice_worker.checkpoint(next_run, first, "features", lambda: pytest.fail("repeated stage"))
