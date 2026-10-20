from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest


def _checker(tmp_path, monkeypatch):
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
    package = {"ecosystem": "python", "name": "example", "version": "1.0", "integrity": "sha256:abc"}
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
