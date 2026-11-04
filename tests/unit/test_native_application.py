"""Native delivery keeps source, credentials and process ownership independently verifiable."""
from __future__ import annotations

import json
import sys

import pytest

if sys.platform == "win32":
    pytest.skip("Delivery fixtures use the Linux development Harness", allow_module_level=True)

from chatcopilot.core.application_installation import application_identity, installation
from chatcopilot.deployment.application_bundle import MANIFEST, NATIVE_SCHEMA, read_bundle
from chatcopilot.deployment.application_models import model_settings
from chatcopilot.deployment.application_runtime import install_bundle
from tests.unit.test_installed_application import application_bundle as _bundle_fixture, installed as _installed_fixture


@pytest.fixture
def installed(tmp_path):
    return _installed_fixture.__wrapped__(tmp_path)


@pytest.fixture
def application_bundle(installed, tmp_path):
    return _bundle_fixture.__wrapped__(installed, tmp_path)


@pytest.fixture
def native_bundle(application_bundle):
    from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
    bundle, source = application_bundle
    required = ("integrations/live2d_desktop/scripts/bootstrap-windows.ps1",
        "integrations/live2d_desktop/windows-runtime.lock.json", "integrations/live2d_desktop/windows-host-requirements.lock",
        "integrations/open_llm_vtuber/agentstrata_vtuber/resources/requirements-windows.lock",
        "src/chatcopilot/deployment/windows_runtime.py")
    for name in required:
        path = bundle / "source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic native installation input\n")
    # This fixture deliberately asserts build provenance, never independent acceptance.
    manifest = source_manifest(bundle / "source") if (bundle / "source/.git").exists() else {
        name: row for name, row in source_manifest(source).items()}
    import hashlib
    for path in (bundle / "source").rglob("*"):
        if path.is_file():
            manifest[path.relative_to(bundle / "source").as_posix()] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "executable": bool(path.stat().st_mode & 0o111)}
    record = json.loads((bundle / MANIFEST).read_text())
    record.update(schema=NATIVE_SCHEMA, platform="windows-x86_64",
        origin={"kind": "source-build", "base_revision": "a" * 40},
        source_manifest=manifest, source_digest=manifest_digest(manifest))
    record.pop("accepted_candidate")
    (bundle / "install.sh").unlink()
    artifacts = bundle / "artifacts.json"
    artifacts.write_text(json.dumps({"source_digest": record["source_digest"], "files": {}}))
    record["artifacts_digest"] = hashlib.sha256(artifacts.read_bytes()).hexdigest()
    (bundle / MANIFEST).write_text(json.dumps(record))
    return bundle


def test_native_installation_does_not_require_git_or_producer(native_bundle, tmp_path, monkeypatch):
    destination = tmp_path / "native"
    monkeypatch.setattr("chatcopilot.deployment.application_runtime._initialize_source",
                        lambda *_: pytest.fail("Native users must not require Git"))
    install_bundle(native_bundle, destination)
    assert not (destination / ".git").exists()
    expected = read_bundle(native_bundle)["source_digest"]
    assert application_identity(destination)["source_digest"] == expected
    assert install_bundle(native_bundle, destination)["reused"]
    (destination / "src/chatcopilot/application_cli.py").write_text("# changed\n")
    with pytest.raises(ValueError, match="identity"):
        application_identity(destination)


def test_native_build_cannot_claim_accepted_candidate(native_bundle):
    path = native_bundle / MANIFEST
    record = json.loads(path.read_text())
    record["accepted_candidate"] = {"candidate_digest": record["source_digest"]}
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="source-build"):
        read_bundle(native_bundle)


def test_native_source_rejects_unrecorded_code(native_bundle, tmp_path):
    root = tmp_path / "unexpected-code"
    install_bundle(native_bundle, root)
    (root / "src/chatcopilot/unrecorded.py").write_text("# not part of this build\n")
    with pytest.raises(ValueError, match="unrecorded"):
        application_identity(root)


@pytest.mark.parametrize("name", ["src/CON", "src/a:b", "src/file.", "src/file "])
def test_native_manifest_rejects_windows_path_aliases(native_bundle, name):
    path = native_bundle / MANIFEST
    record = json.loads(path.read_text())
    record["source_manifest"][name] = {"sha256": "a" * 64, "executable": False}
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="Windows path alias"):
        read_bundle(native_bundle)


def test_application_model_credentials_are_not_returned_or_changed_by_stale_edits(native_bundle, tmp_path):
    root = tmp_path / "models"
    install_bundle(native_bundle, root)
    initial = model_settings(root)
    first = model_settings(root, {"kind": "openai_compatible", "model": "fixture-model",
        "base_url": "https://fixture.invalid/v1", "api_key": "fixture-credential", "revision": initial["revision"]})
    assert "fixture-credential" not in json.dumps(first)
    secret = next((installation(root).control / "model-credentials").iterdir())
    with pytest.raises(ValueError):
        model_settings(root, {"kind": "openai_compatible", "model": "fixture-model",
            "base_url": "https://fixture.invalid/v1", "api_key": "stale-credential", "revision": initial["revision"]})
    assert "stale-credential" not in secret.read_text()
    second = model_settings(root, {"kind": "chatgpt", "model": "fixture-model", "revision": first["revision"]})
    assert second["kind"] == "chatgpt"
    assert secret.exists()

