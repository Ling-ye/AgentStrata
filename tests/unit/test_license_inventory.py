from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest


def _checker(tmp_path, monkeypatch, *, lockfiles=False):
    path = Path(__file__).resolve().parents[2] / "scripts/third_party_notices.py"
    spec = importlib.util.spec_from_file_location("license_inventory_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "INDEX", tmp_path / "inventory.json")
    monkeypatch.setattr(module, "NOTICES", tmp_path / "notices.txt")
    (tmp_path / "third_party").mkdir()
    (tmp_path / "third_party/license_sources.json").write_text("{}")
    module.NOTICES.write_text("Original license text")
    package = {"ecosystem": "python", "name": "example", "version": "1.0", "integrity": "sha256:abc", "scopes": ["dev"]}
    if not lockfiles:
        monkeypatch.setattr(module, "dependencies", lambda: [package.copy()])
    index = {"notices_sha256": hashlib.sha256(module.NOTICES.read_bytes()).hexdigest(),
             "reviewed_sources_sha256": hashlib.sha256(b"{}").hexdigest(),
             "dependencies": [{**package, "license_files": {"LICENSE": "sha256-example"}}]}
    module.INDEX.write_text(json.dumps(index))
    return module


def test_inventory_checks_lock_and_reviewed_material_integrity(tmp_path, monkeypatch):
    checker = _checker(tmp_path, monkeypatch)
    checker.check()
    index = json.loads(checker.INDEX.read_text())
    index["dependencies"][0]["version"] = "2.0"
    checker.INDEX.write_text(json.dumps(index))
    with pytest.raises(ValueError, match="differs from lockfiles"):
        checker.check()


def test_altered_license_text_is_rejected(tmp_path, monkeypatch):
    checker = _checker(tmp_path, monkeypatch)
    checker.NOTICES.write_text("Replaced copyright notice")
    with pytest.raises(ValueError, match="does not match"):
        checker.check()


def test_unresolved_material_cannot_pass_release_check(tmp_path, monkeypatch):
    checker = _checker(tmp_path, monkeypatch)
    index = json.loads(checker.INDEX.read_text())
    index["dependencies"][0]["reviewed_evidence"] = {"review_status": "pending-maintainer-decision"}
    checker.INDEX.write_text(json.dumps(index))
    with pytest.raises(ValueError, match="awaits maintainer"):
        checker.check()


def test_archive_reader_does_not_execute_package_code(tmp_path, monkeypatch):
    checker = _checker(tmp_path, monkeypatch)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("setup.py", "raise RuntimeError('package code must not run')")
        archive.writestr("pkg.dist-info/licenses/LICENSE", "original notice")
    assert checker._members(data.getvalue(), "https://example.org/package.whl") == {
        "pkg.dist-info/licenses/LICENSE": b"original notice"}


def test_reviewed_bundle_copyright_headers_survive_minification(tmp_path, monkeypatch):
    checker = _checker(tmp_path, monkeypatch)
    source = b"/* Copyright original authors. SPDX-License-Identifier: MIT */\nthrow Error('must not run');"
    evidence = {"npm:example:1.0": {"copyright_notice_files": ["package/bundle.js"]}}
    (tmp_path / "third_party/license_sources.json").write_text(json.dumps(evidence))
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("package/package.json", '{"name":"example","license":"MIT"}')
        archive.writestr("package/LICENSE", "Original MIT license")
        archive.writestr("package/bundle.js", source)
    monkeypatch.setattr(checker, "_download", lambda *_args: data.getvalue())
    monkeypatch.setattr(checker, "_read_url", lambda *_args: json.dumps({"gitHead": "a" * 40,
        "repository": {"url": "https://github.com/example/sample.git"}}).encode())
    record, notice = checker.inspect_package({"ecosystem": "npm", "name": "example", "version": "1.0", "license": "MIT",
        "artifact_url": "https://example.org/package.tgz", "integrity": "sha256:example", "scopes": ["runtime"],
        "source_url": "https://example.org/source", "source_integrity": "sha256:example"})
    assert "Copyright original authors. SPDX-License-Identifier: MIT" in notice
    assert "must not run" not in notice
    assert record["notice_sources"] == {"package/bundle.js": hashlib.sha256(source).hexdigest()}
    assert record["preferred_source_url"] == "https://github.com/example/sample/archive/" + "a" * 40 + ".tar.gz"


def _lockfiles(root, *, uv_version="1.0", build_hash="a" * 64):
    (root / "uv.lock").write_text(f'''
[[package]]
name = "agentstrata"
version = "1.0"
[package.optional-dependencies]
dev = [{{name = "example"}}]
[[package]]
name = "example"
version = "{uv_version}"
wheels = [{{url = "https://files.pythonhosted.org/example-{uv_version}.whl", hash = "sha256:{'b' * 64}", size = 100}}]
''')
    (root / "requirements").mkdir()
    (root / "requirements/release-build.txt").write_text(f"example==2.0 \\\n    --hash=sha256:{build_hash}\n")
    (root / "console/web").mkdir(parents=True)
    (root / "console/web/package-lock.json").write_text('{"packages":{"":{}}}')


def test_independent_build_version_is_required_by_offline_inventory(tmp_path, monkeypatch):
    checker = _checker(tmp_path, monkeypatch, lockfiles=True)
    _lockfiles(tmp_path)
    monkeypatch.setattr(checker, "_read_url", lambda *_args: pytest.fail("offline check accessed the network"))
    packages = checker.dependencies()
    assert [(p["version"], p["scopes"]) for p in packages] == [("1.0", ["dev"]), ("2.0", ["release-build"])]
    assert packages[1]["release_build_hashes"] == ["sha256:" + "a" * 64]
    index = json.loads(checker.INDEX.read_text())
    index["dependencies"] = [{**packages[0], "license_files": {"LICENSE": "example"}}]
    checker.INDEX.write_text(json.dumps(index))
    with pytest.raises(ValueError, match="differs from lockfiles"):
        checker.check()
    index["dependencies"].append({**packages[1], "integrity": "sha256:" + "b" * 64, "license_files": {"LICENSE": "example"}})
    checker.INDEX.write_text(json.dumps(index))
    with pytest.raises(ValueError, match="does not match its locked hashes"):
        checker.check()


@pytest.mark.parametrize("uv_version", ["1.0", "2.0"])
def test_build_refresh_inspects_hash_locked_archive_even_outside_uv(tmp_path, monkeypatch, uv_version):
    checker = _checker(tmp_path, monkeypatch, lockfiles=True)
    monkeypatch.setattr(checker, "tempfile", SimpleNamespace(gettempdir=lambda: str(tmp_path)))
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("example-2.0.dist-info/METADATA", "Name: example\nVersion: 2.0\nLicense-Expression: MIT\n")
        archive.writestr("example-2.0.dist-info/LICENSE", "Original license for build version 2.0")
    digest = hashlib.sha256(data.getvalue()).hexdigest()
    _lockfiles(tmp_path, uv_version=uv_version, build_hash=digest)
    archive_url = "https://files.pythonhosted.org/example-2.0.whl"
    calls = []

    def read_url(url):
        calls.append(url)
        if url == archive_url:
            return data.getvalue()
        assert url == "https://pypi.org/pypi/example/2.0/json"
        return json.dumps({"urls": [{"url": archive_url, "digests": {"sha256": digest},
                                    "size": len(data.getvalue()), "packagetype": "bdist_wheel"}]}).encode()

    monkeypatch.setattr(checker, "_read_url", read_url)
    packages = checker.dependencies(resolve_build_artifacts=True)
    build = next(p for p in packages if "release-build" in p["scopes"])
    assert build["integrity"] == "sha256:" + digest
    record, notice = checker.inspect_package(build)
    assert record["license"] == "MIT" and record["license_files"]
    assert "Original license for build version 2.0" in notice
    assert record["scopes"] == (["dev", "release-build"] if uv_version == "2.0" else ["release-build"])
    assert calls[0] == "https://pypi.org/pypi/example/2.0/json"
    # A fresh audit is sufficient for subsequent offline checks, even when uv
    # has no record of this build version or the selected wheel hash.
    index = json.loads(checker.INDEX.read_text())
    index["dependencies"] = [record if "release-build" in p["scopes"] else {
        **p, "license_files": {"LICENSE": "example"}} for p in packages]
    checker.INDEX.write_text(json.dumps(index))
    monkeypatch.setattr(checker, "_read_url", lambda *_args: pytest.fail("offline check accessed the network"))
    checker.check()
    build_record = next(p for p in index["dependencies"] if "release-build" in p["scopes"])
    del build_record["source_url"]
    checker.INDEX.write_text(json.dumps(index))
    with pytest.raises(ValueError, match="archive/source evidence"):
        checker.check()


def test_build_hash_absent_from_pypi_is_rejected(tmp_path, monkeypatch):
    checker = _checker(tmp_path, monkeypatch, lockfiles=True)
    _lockfiles(tmp_path)
    monkeypatch.setattr(checker, "_read_url", lambda *_args: b'{"urls":[]}')
    with pytest.raises(ValueError, match="no PyPI archive matches"):
        checker.dependencies(resolve_build_artifacts=True)


@pytest.mark.parametrize("pin", ["example>=2.0", "example==2.0", "example==2.0 --hash=sha256:invalid"])
def test_unpinned_build_requirement_cannot_silently_disappear(tmp_path, monkeypatch, pin):
    checker = _checker(tmp_path, monkeypatch, lockfiles=True)
    _lockfiles(tmp_path)
    (tmp_path / "requirements/release-build.txt").write_text(pin)
    with pytest.raises(ValueError, match="exact versions and SHA-256 hashes"):
        checker.dependencies()
