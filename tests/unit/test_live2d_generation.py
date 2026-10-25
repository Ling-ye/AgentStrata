from __future__ import annotations

import base64
import json
from pathlib import Path
import stat
import sys
import zipfile

import pytest

from integrations.live2d_generator.agentstrata_live2d.config import Config
from integrations.live2d_generator.agentstrata_live2d.repository import (
    JobRepository, extract_zip, validate_bundle, write_json,
)
from integrations.live2d_generator.agentstrata_live2d.service import GenerationService
from integrations.live2d_generator.agentstrata_live2d.types import GenerationError, GenerationRequest


def png(path: Path, size=(32, 48)):
    Image = pytest.importorskip("PIL.Image")
    Image.new("RGBA", size, (80, 120, 200, 255)).save(path)


def bundle(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    # Deliberately only a format fixture; this test does not claim SDK validation.
    (root / "avatar.moc3").write_bytes(b"MOC3\x01" + b"\0" * 59)
    png(root / "texture.png")
    png(root / "preview.png")
    write_json(root / "neutral.exp3.json", {"Type": "Live2D Expression", "Parameters": []})
    write_json(root / "idle.motion3.json", {"Version": 3, "Curves": []})
    write_json(root / "avatar.model3.json", {"Version": 3, "FileReferences": {
        "Moc": "avatar.moc3", "Textures": ["texture.png"],
        "Expressions": [{"Name": "neutral", "File": "neutral.exp3.json"}],
        "Motions": {"Idle": [{"File": "idle.motion3.json"}]}}})
    return root


def archive(path: Path, entries: dict):
    with zipfile.ZipFile(path, "w") as stream:
        for name, data in entries.items():
            stream.writestr(name, data)
    return path


def archive_bundle(path: Path, root: Path):
    return archive(path, {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()})


@pytest.mark.parametrize("value", [
    {"name": ""}, {"name": "a", "endpoint": "https://attacker.invalid"},
    {"name": "a", "information": {"age": 3}}, {"name": "a", "redraw": "yes"},
    {"name": "a", "prompt": "x" * 12001}, {"name": "a", "information": []},
])
def test_request_rejects_invalid_or_executable_fields(value):
    with pytest.raises(GenerationError):
        GenerationRequest.parse(value)


def test_information_is_input_to_image_prompt():
    request = GenerationRequest.parse({"name": "角色", "information": {"发色": "银白"}})
    assert "发色: 银白" in request.image_prompt()
    assert request.redraw is False


@pytest.mark.parametrize("value", [
    {"decompose_url": "http://public.example"}, {"decompose_url": "https://" + "user" + ":" + "secret" + "@example.com"},
    {"decompose_url": "https://example.com" + "?" + "token=" + "secret"}, {"image_base_url": "https://example.com/v1"},
    {"image_key_env": "actual-secret-value"}, {"port": True}, {"resolution": 999999},
    {"decompose_space": "owner/space", "decompose_url": "http://127.0.0.1:8000"},
    {"decompose_space": "../path/to/script"}, {"command": "touch /tmp/owned"},
])
def test_configuration_is_operator_only_and_credential_free(value):
    with pytest.raises(GenerationError):
        Config.parse(value)


@pytest.mark.parametrize("name", ["../outside.png", "/tmp/outside.png", "a/../../outside.png",
                                 "C:/outside.png", "a\\outside.png", "a//image.png", "a/%2e%2e/image.png"])
def test_archive_cannot_escape(tmp_path, name):
    source = archive(tmp_path / "source.zip", {name: b"bad"})
    with pytest.raises(GenerationError):
        extract_zip(source, tmp_path / "target", suffixes={".png"})
    assert not (tmp_path / "outside.png").exists()


def test_archive_rejects_link_and_duplicate(tmp_path):
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as stream:
        link = zipfile.ZipInfo("link.png")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        stream.writestr(link, "../../secret")
    with pytest.raises(GenerationError):
        extract_zip(path, tmp_path / "target", suffixes={".png"})
    archive(path, {"Face.png": b"a", "face.png": b"b"})
    with pytest.raises(GenerationError):
        extract_zip(path, tmp_path / "target2", suffixes={".png"})


def test_bundle_requires_actual_moc_textures_and_safe_references(tmp_path):
    root = bundle(tmp_path / "model")
    result = validate_bundle(root)
    assert result["expressions"] == {"neutral": 0}
    assert result["motions"] == {"idle_0": {"group": "Idle", "index": 0}}
    assert result["validation"] == "file-integrity"
    (root / "avatar.moc3").unlink()
    with pytest.raises(GenerationError, match="缺失"):
        validate_bundle(root)
    (root / "avatar.moc3").write_bytes(b"not a moc")
    with pytest.raises(GenerationError, match="MOC3"):
        validate_bundle(root)


@pytest.mark.parametrize("reference", ["../secret.png", "https://example.com/image.png", "file:///etc/passwd"])
def test_model_references_do_not_fetch_external_files(tmp_path, reference):
    root = bundle(tmp_path / "model")
    doc = json.loads((root / "avatar.model3.json").read_text())
    doc["FileReferences"]["Textures"] = [reference]
    write_json(root / "avatar.model3.json", doc)
    with pytest.raises(GenerationError):
        validate_bundle(root)


def test_bundle_rejects_even_unreferenced_links(tmp_path):
    root = bundle(tmp_path / "model")
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(GenerationError, match="符号链接"):
        validate_bundle(root)


def service_fixture(tmp_path, *, failure=None):
    calls = []

    class Image:
        def generate(self, prompt, reference, destination):
            calls.append(("image", reference, prompt))
            if failure:
                raise failure
            png(destination)

    class Layers:
        def decompose(self, image, destination, check):
            calls.append(("layers", image))
            check()
            destination.write_bytes(b"fake layered PSD for provider contract test")

    class Rig:
        def build(self, source, destination, work):
            calls.append(("rig", source))
            bundle(destination)
            return {"layers": 4, "parameters": 2, "sdk_validation": "not-run"}

    return GenerationService(JobRepository(tmp_path / "jobs"), Image(), Layers(), Rig()), calls


@pytest.mark.parametrize("kind", ["prompt", "information", "image", "redraw", "psd", "zip"])
def test_input_routes_share_one_verified_package_pipeline(tmp_path, kind):
    service, calls = service_fixture(tmp_path)
    source = None
    if kind in {"image", "redraw", "psd", "zip"}:
        source = tmp_path / ("input." + (kind if kind in {"psd", "zip"} else "png"))
        if source.suffix == ".png":
            png(source)
        else:
            source.write_bytes(b"fixture")
    request = GenerationRequest("角色", prompt="yellow coat" if kind in {"prompt", "redraw"} else "",
                                information={"hair": "silver"} if kind == "information" else {},
                                redraw=kind == "redraw")
    identifier = service.submit(request, source)
    state = service.run(identifier)
    assert state["status"] == "completed"
    expected = ["image", "layers", "rig"] if kind in {"prompt", "information", "redraw"} else (
        ["layers", "rig"] if kind == "image" else ["rig"])
    assert [c[0] for c in calls] == expected
    assert (service.repository.path(identifier) / "model.zip").is_file()
    assert state["result"]["generation"]["sdk_validation"] == "not-run"
    if kind == "redraw":
        assert calls[0][1] is not None


def test_failure_redacts_provider_body_and_does_not_publish(tmp_path):
    service, _ = service_fixture(tmp_path, failure=RuntimeError("api_key=PRIVATE_VALUE"))
    identifier = service.submit(GenerationRequest("角色", "hello"), None)
    state = service.run(identifier)
    assert state["status"] == "failed"
    assert "PRIVATE_VALUE" not in json.dumps(state)
    assert not (service.repository.path(identifier) / "model.zip").exists()


def test_cancel_and_repeated_execution_cannot_publish(tmp_path):
    service, calls = service_fixture(tmp_path)
    identifier = service.submit(GenerationRequest("角色", "hello"), None)
    assert service.run(identifier, cancelled=lambda: True)["status"] == "cancelled"
    assert calls == []
    with pytest.raises(GenerationError):
        service.run(identifier)


def test_runtime_interrupts_stale_jobs_and_locks_directory(tmp_path):
    from integrations.live2d_generator.agentstrata_live2d.runtime import JobRuntime

    service, _ = service_fixture(tmp_path)
    identifier = service.submit(GenerationRequest("old", "hello"), None)
    with JobRuntime(service.repository.root, Config()) as runtime:
        assert runtime.get(identifier)["status"] == "failed"
        with pytest.raises(GenerationError):
            with JobRuntime(service.repository.root, Config()):
                pass


def test_provider_api_sends_reference_as_edit_without_response_format(tmp_path, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d import providers

    reference = tmp_path / "reference.png"
    png(reference)
    calls = []

    def response(client, method, url, **kwargs):
        calls.append((url, kwargs))
        return json.dumps({"data": [{"b64_json": base64.b64encode(reference.read_bytes()).decode()}]}).encode()

    monkeypatch.setattr(providers, "response_bytes", response)
    config = Config(image_base_url="https://images.example/v1", image_model="configured-model")
    provider = providers.ImagesAPI(config)
    provider.generate("brief", None, tmp_path / "generated.png")
    provider.generate("redraw", reference, tmp_path / "edited.png")
    assert calls[0][0].endswith("/images/generations")
    assert calls[1][0].endswith("/images/edits")
    assert "response_format" not in calls[0][1]["json"]
    assert "image" in calls[1][1]["files"]


def test_web_auth_precedes_body_parse_and_task_access(tmp_path):
    from fastapi.testclient import TestClient
    from integrations.live2d_generator.agentstrata_live2d.runtime import JobRuntime
    from integrations.live2d_generator.agentstrata_live2d.web import create_app
    import re

    runtime = JobRuntime(tmp_path / "jobs", Config())
    with TestClient(create_app(runtime), base_url="http://127.0.0.1:12394") as client:
        assert client.get("/api/status").status_code == 403
        assert client.post("/api/jobs", content=b"not JSON").status_code == 403
        assert client.get("/", headers={"host": "attacker.example"}).status_code == 403
        assert client.get("/", headers={"origin": "https://attacker.example"}).status_code == 403
        page = client.get("/")
        token = re.search(r'name="generator-token" content="([^"]+)"', page.text)[1]
        headers = {"x-generator-token": token}
        assert client.get("/api/status", headers=headers).json()["jobs"] == []
        assert client.post("/api/jobs", json={"request": {"name": "hello", "prompt": "world"}},
                           headers=headers).status_code == 400
        assert client.get("/api/jobs/invalid", headers=headers).status_code == 400
        assert page.headers["x-frame-options"] == "DENY"
        assert runtime.list() == []


def vtuber_target(root, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d import install
    # Filesystem handoff tests use an explicitly stopped test target; systemd is tested separately.
    monkeypatch.setattr(install, "require_stopped", lambda *args: None)
    from integrations.live2d_generator.agentstrata_live2d.repository import private_directory
    private_directory(root / "config")
    private_directory(root / "data/live2d/models")
    write_json(root / "installation.json", {"plugin_id": "open-llm-vtuber", "root": str(root),
                                           "status": "stopped", "services": [{"name": "client", "port": 19893},
                                              {"name": "gateway", "port": 19894}]})
    write_json(root / "data/live2d/model_dict.json", [{"name": "mao_pro"}])
    write_json(root / "config/adapter.json", {"model_name": "mao_pro", "gateway_token_env": "KEEP_ME"})
    write_json(root / "config/upstream.yaml", {"character_config": {"character_name": "Mao",
                                                                   "live2d_model_name": "mao_pro"}})
    return root


def test_install_restore_preserves_agent_configuration_and_source(tmp_path, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d.install import install_bundle, restore_bundle, CONFIG_FILES

    target = vtuber_target(tmp_path / "client", monkeypatch)
    before = {p: (target / p).read_bytes() for p in CONFIG_FILES}
    source = archive_bundle(tmp_path / "model.zip", bundle(tmp_path / "model"))
    result = install_bundle(source, target)
    adapter = json.loads((target / "config/adapter.json").read_text())
    assert adapter["gateway_token_env"] == "KEEP_ME"
    assert adapter["model_name"] == result["model"]
    assert adapter["expressions"] == {"neutral": 0}
    assert (target / "data/live2d/models" / result["model"] / "avatar.moc3").exists()
    restore_bundle(target, result["backup"])
    assert before == {p: (target / p).read_bytes() for p in CONFIG_FILES}


def test_restore_refuses_to_overwrite_later_edits(tmp_path, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d.install import install_bundle, restore_bundle

    target = vtuber_target(tmp_path / "client", monkeypatch)
    source = archive_bundle(tmp_path / "model.zip", bundle(tmp_path / "model"))
    result = install_bundle(source, target)
    write_json(target / "config/adapter.json", {"user_edit": True})
    with pytest.raises(GenerationError, match="后续修改"):
        restore_bundle(target, result["backup"])
    assert json.loads((target / "config/adapter.json").read_text()) == {"user_edit": True}


def test_install_rolls_back_partial_configuration_failure(tmp_path, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d import install

    target = vtuber_target(tmp_path / "client", monkeypatch)
    before = {p: (target / p).read_bytes() for p in install.CONFIG_FILES}
    source = archive_bundle(tmp_path / "model.zip", bundle(tmp_path / "model"))
    original = install.write_bytes
    failed = False

    def fail_once(path, value):
        nonlocal failed
        if path == target / "config/adapter.json" and not failed:
            failed = True
            raise OSError("simulated disk failure")
        return original(path, value)

    monkeypatch.setattr(install, "write_bytes", fail_once)
    with pytest.raises(OSError):
        install.install_bundle(source, target)
    assert before == {p: (target / p).read_bytes() for p in install.CONFIG_FILES}
    assert not (target / "config/pending-model-import.json").exists()


def test_generator_does_not_import_runtime_domains():
    import ast

    root = Path(__file__).parents[2] / "integrations/live2d_generator/agentstrata_live2d"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([node.module or ""] if isinstance(node, ast.ImportFrom) else
                     [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            assert not any(n.startswith(("chatcopilot.agent", "chatcopilot.gateway", "agentstrata_vtuber")) for n in names)


def test_empty_input_and_invalid_upload_leave_no_job(tmp_path):
    service, _ = service_fixture(tmp_path)
    with pytest.raises(GenerationError):
        service.submit(GenerationRequest("empty"), None)
    with pytest.raises(GenerationError):
        service.submit(GenerationRequest("missing"), tmp_path / "missing.png")
    assert service.repository.list() == []


def test_http_decomposer_uses_one_submission_and_rejects_unsafe_job_id(tmp_path, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d import providers
    image = tmp_path / "image.png"
    image.write_bytes(b"fixture")
    calls = []
    responses = iter([b'{"job_id":"../../secrets"}'])

    def response(client, method, url, **kwargs):
        calls.append((method, url))
        return next(responses)

    monkeypatch.setattr(providers, "response_bytes", response)
    provider = providers.SeeThroughHTTP(Config(decompose_url="http://127.0.0.1:8000"))
    with pytest.raises(GenerationError, match="协议数据"):
        provider.decompose(image, tmp_path / "output.psd", lambda: None)
    assert len(calls) == 1
    assert calls[0][0] == "POST"


def test_archive_file_count_is_bounded(tmp_path):
    path = archive(tmp_path / "source.zip", {f"{i}.png": b"x" for i in range(257)})
    with pytest.raises(GenerationError, match="256"):
        extract_zip(path, tmp_path / "out", suffixes={".png"})


def test_client_assets_are_persistent_and_pending_import_blocks_start(tmp_path):
    from integrations.open_llm_vtuber.agentstrata_vtuber.assets import prepare_assets, runtime_assets
    from chatcopilot.core.install_artifacts import InstallError
    backend = tmp_path / "backend"
    (backend / "live2d-models").mkdir(parents=True)
    (backend / "live2d-models/model.txt").write_text("fixed source")
    (backend / "model_dict.json").write_text('[{"name":"default"}]')
    root = tmp_path / "client"
    prepare_assets(root, backend)
    assert runtime_assets(root)["AGENTSTRATA_VTUBER_MODELS"] == str(root / "data/live2d/models")
    write_json(root / "data/live2d/model_dict.json", [{"name": "custom"}])
    prepare_assets(root, backend)
    assert json.loads((root / "data/live2d/model_dict.json").read_text()) == [{"name": "custom"}]
    write_json(root / "config/pending-model-import.json", {"backup": "fixture"})
    with pytest.raises(InstallError, match="Interrupted"):
        runtime_assets(root)
    assert (backend / "model_dict.json").read_text() == '[{"name":"default"}]'


def test_running_client_is_not_modified(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from integrations.live2d_generator.agentstrata_live2d import install
    check_stopped = install.require_stopped
    target = vtuber_target(tmp_path / "client", monkeypatch)
    monkeypatch.setattr(install, "require_stopped", check_stopped)
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="active\n"))
    before = {p: (target / p).read_bytes() for p in install.CONFIG_FILES}
    with pytest.raises(GenerationError, match="停止"):
        install.install_bundle(tmp_path / "unused.zip", target)
    assert before == {p: (target / p).read_bytes() for p in install.CONFIG_FILES}


def test_manifest_hash_detects_modified_texture(tmp_path):
    root = bundle(tmp_path / "model")
    checked = validate_bundle(root)
    write_json(root / "generation.json", {"format": "agentstrata-live2d-v1", "model": checked["model"],
                                           "files": checked["sha256"]})
    validate_bundle(root)
    png(root / "texture.png", size=(48, 48))
    with pytest.raises(GenerationError, match="摘要不一致"):
        validate_bundle(root)


def test_space_download_is_bounded_same_origin_and_disables_implicit_identity(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from integrations.live2d_generator.agentstrata_live2d import providers
    calls = []
    clients = []

    class Client:
        src = "https://space.example.com/"
        headers = {}
        def __init__(self, name, **kwargs):
            calls.append(kwargs)
            clients.append(self)
            self.closed = False
        def submit(self, *args, **kwargs):
            return SimpleNamespace(done=lambda: True,
                                   result=lambda: [{"url": self.src + "gradio_api/file=output.psd"}, []])
        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "gradio_client", SimpleNamespace(Client=Client, handle_file=str))
    monkeypatch.setattr(providers, "response_bytes", lambda *args, **kwargs: b"PSD fixture")
    provider = providers.SeeThroughSpace(Config(decompose_space="fixture/space"))
    provider.decompose(tmp_path / "image.png", tmp_path / "output.psd", lambda: None)
    assert (tmp_path / "output.psd").read_bytes() == b"PSD fixture"
    assert calls[0]["token"] is False and calls[0]["download_files"] is False
    assert calls[0]["analytics_enabled"] is False and clients[0].closed


def test_actions_match_the_existing_client_identifier_contract(tmp_path):
    root = bundle(tmp_path / "model")
    doc = json.loads((root / "avatar.model3.json").read_text())
    doc['FileReferences']['Expressions'][0]['Name'] = 'Smile Face'
    doc['FileReferences']['Motions']['挥手动作'] = doc['FileReferences']['Motions']['Idle']
    write_json(root / "avatar.model3.json", doc)
    actions = validate_bundle(root)
    import re
    assert all(re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name)
               for name in [*actions['expressions'], *actions['motions']])
    assert actions['motions']['idle_0'] == {'group': 'Idle', 'index': 0}
    assert any(v['group'] == '挥手动作' for v in actions['motions'].values())


def test_source_notice_exposes_no_credential_url(monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d.config import source_notice
    monkeypatch.delenv('CHATCOPILOT_SOURCE_URL', raising=False)
    assert not source_notice()['source_configured']
    monkeypatch.setenv('CHATCOPILOT_SOURCE_URL', 'https://example.com/source.tar.gz')
    assert source_notice()['source_configured']
    assert source_notice()['source_url'].endswith('source.tar.gz')
    monkeypatch.setenv('CHATCOPILOT_SOURCE_URL', 'https://' + 'user:' + 'secret' + '@example.com/source')
    assert not source_notice()['source_configured']
    assert 'secret' not in json.dumps(source_notice())


@pytest.mark.parametrize("side", ["l", "r"])
def test_psd_eye_order_keeps_iris_visible_and_preserves_hair_occlusion(side):
    from types import SimpleNamespace
    from integrations.live2d_generator.agentstrata_live2d.rigging import order_eye_layers
    Image = pytest.importorskip("PIL.Image")
    white = Image.new("RGBA", (5, 5), "white")
    pupil = Image.new("RGBA", (5, 5))
    pupil.putpixel((2, 2), (110, 70, 40, 255))
    lashes = Image.new("RGBA", (5, 5))
    lashes.putpixel((2, 0), (0, 0, 0, 255))
    hair = Image.new("RGBA", (5, 5))
    hair.putpixel((4, 2), (200, 180, 80, 255))
    def layer(role, order, image):
        return SimpleNamespace(semantic_role=SimpleNamespace(value=role), draw_order=order, image=image)
    layers = [layer(f"eye_{side}", 1, lashes), layer(f"pupil_{side}", 2, pupil),
              layer(f"eye_white_{side}", 3, white), layer("hair_front", 4, hair)]
    def render():
        image = Image.new("RGBA", (5, 5))
        for part in sorted(layers, key=lambda part: part.draw_order):
            image.alpha_composite(part.image)
        return image
    assert render().getpixel((2, 2)) == (255, 255, 255, 255)
    order_eye_layers(SimpleNamespace(layers=layers))
    result = render()
    assert result.getpixel((2, 2)) == (110, 70, 40, 255)
    assert result.getpixel((2, 0)) == (0, 0, 0, 255)
    assert result.getpixel((4, 2)) == (200, 180, 80, 255)
    assert layers[-1].draw_order == 4


@pytest.mark.parametrize("missing", ["eye_white_l", "pupil_l"])
def test_psd_eye_order_preserves_incomplete_and_already_ordered_eyes(missing):
    from types import SimpleNamespace
    from integrations.live2d_generator.agentstrata_live2d.rigging import order_eye_layers
    values = [("pupil_l", 1), ("eye_l", 3), ("eye_white_l", 4),
              ("eye_white_r", 10), ("pupil_r", 11), ("eye_r", 12), ("accessory", 5)]
    layers = [SimpleNamespace(semantic_role=SimpleNamespace(value=role), draw_order=order)
              for role, order in values if role != missing]
    before = [part.draw_order for part in layers]
    order_eye_layers(SimpleNamespace(layers=layers))
    assert [part.draw_order for part in layers] == before
