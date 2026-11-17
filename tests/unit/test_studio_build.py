"""Studio release archives carry freshly built assets, without using checkout dist."""
import hashlib
from pathlib import Path
import subprocess
import tarfile

import pytest

from studio import build


@pytest.fixture
def release(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    source = root / "app.py"
    source.write_text("current application")
    stale = root / "console/web/dist"
    stale.mkdir(parents=True)
    (stale / "index.html").write_text("stale checkout page")
    calls = []

    def manifest(_root):
        return {"app.py": {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "executable": False}}

    def compile_frontend(argv, *, check):
        assert check and argv[:3] == ["npm", "run", "build"]
        calls.append(argv)
        frontend = Path(argv[argv.index("--outDir") + 1])
        frontend.mkdir()
        (frontend / "index.html").write_text('<script src="/assets/current.js"></script>')
        (frontend / "assets").mkdir()
        (frontend / "assets/current.js").write_text("current application frontend")

    monkeypatch.setattr(build, "source_manifest", manifest)
    monkeypatch.setattr(build.subprocess, "run", compile_frontend)
    return root, tmp_path / "framework.tar.gz", source, stale, calls, compile_frontend


def test_release_contains_current_source_and_fresh_frontend_without_mutating_checkout(release):
    root, archive, _, stale, calls, _ = release
    build.studio_archive(root, archive)
    with tarfile.open(archive) as stream:
        assert set(stream.getnames()) == {"app.py", "console/web/dist/index.html", "console/web/dist/assets/current.js"}
        assert stream.extractfile("app.py").read() == b"current application"
        assert b"current.js" in stream.extractfile("console/web/dist/index.html").read()
        assert stream.extractfile("console/web/dist/assets/current.js").read() == b"current application frontend"
    assert (stale / "index.html").read_text() == "stale checkout page"
    assert len(calls) == 1


def test_failed_frontend_build_does_not_create_source_only_release(release, monkeypatch):
    root, archive, _, _, _, _ = release

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["npm", "run", "build"])

    monkeypatch.setattr(build.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        build.studio_archive(root, archive)
    assert not archive.exists()


def test_successful_build_without_frontend_index_is_rejected(release, monkeypatch):
    root, archive, _, _, _, _ = release
    monkeypatch.setattr(build.subprocess, "run", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="index.html"):
        build.studio_archive(root, archive)
    assert not archive.exists()


def test_changed_source_during_frontend_build_is_not_packaged(release, monkeypatch):
    root, archive, source, _, _, compile_frontend = release

    def edit_during_build(argv, **kwargs):
        compile_frontend(argv, **kwargs)
        source.write_text("newer application")

    monkeypatch.setattr(build.subprocess, "run", edit_during_build)
    with pytest.raises(ValueError, match="source changed"):
        build.studio_archive(root, archive)
    assert not archive.exists()


def test_existing_archive_is_preserved_without_starting_frontend_build(release):
    root, archive, _, _, calls, _ = release
    archive.write_bytes(b"existing user artifact")
    with pytest.raises(FileExistsError):
        build.studio_archive(root, archive)
    assert archive.read_bytes() == b"existing user artifact" and not calls


def test_linked_frontend_resource_is_not_read_into_release(release, monkeypatch):
    root, archive, source, _, _, compile_frontend = release

    def link_resource(argv, **kwargs):
        compile_frontend(argv, **kwargs)
        frontend = Path(argv[argv.index("--outDir") + 1])
        (frontend / "assets/linked.js").symlink_to(source)

    monkeypatch.setattr(build.subprocess, "run", link_resource)
    with pytest.raises(ValueError, match="regular resources"):
        build.studio_archive(root, archive)
    assert not archive.exists()


def test_source_changed_during_archive_write_discards_the_incomplete_release(release, monkeypatch):
    root, archive, source, _, _, _ = release
    add = tarfile.TarFile.add

    def edit_after_add(self, *args, **kwargs):
        result = add(self, *args, **kwargs)
        source.write_text("changed during archive")
        return result

    monkeypatch.setattr(tarfile.TarFile, "add", edit_after_add)
    with pytest.raises(ValueError, match="source changed"):
        build.studio_archive(root, archive)
    assert not archive.exists()
