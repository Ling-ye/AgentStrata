"""Exercise the public command from a frozen application revision."""
import hashlib
import io
import json
import subprocess
import tarfile

from chatcopilot.__main__ import main
from chatcopilot.core.source_snapshot import git_output


def test_package_uses_only_the_selected_commit_and_repeats_the_linux_artifact(tmp_path, capsys):
    repo = tmp_path / "app"
    app = repo
    repo.mkdir()
    (app / "install.json").write_text(json.dumps({"schema": "agentstrata-install/v1", "recipe": "static"}))
    page = app / "index.html"
    page.write_text("verified release")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git_output(repo, "add", "index.html", "install.json")
    git_output(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    revision = git_output(repo, "rev-parse", "HEAD")
    page.write_text("working tree edit")
    (app / "local.env").write_text("private marker")
    args = ["app", "package", "--source", str(repo), "--revision", revision, "--application", "field-notes",
            "--app-dir", ".", "--platform", "linux-x64", "--output"]
    hashes = []
    for name in ("first", "second"):
        out = tmp_path / name
        assert main([*args, str(out)]) == 0
        record = json.loads(capsys.readouterr().out)
        assert record["source_directory"] == "." and record["recipe"] == "static"
        assert record["origin"] == {"kind": "commit", "revision": revision}
        assert record == json.loads((out / "build-receipt.json").read_text())
        raw = (out / "application.run").read_bytes()
        hashes.append(hashlib.sha256(raw).hexdigest())
        assert hashes[-1] == record["installers"]["linux-x64"]["sha256"]
        with tarfile.open(fileobj=io.BytesIO(raw.split(b"\n__APPLICATION_PAYLOAD__\n", 1)[1]), mode="r:gz") as tar:
            assert tar.extractfile("site/index.html").read() == b"verified release"
            assert "site/local.env" not in tar.getnames()
    assert hashes[0] == hashes[1]
    assert main([*args, str(out)]) == 1
    assert hashlib.sha256((out / "application.run").read_bytes()).hexdigest() == hashes[-1]
    invalid = args.copy()
    invalid[invalid.index(revision)] = "HEAD"
    assert main([*invalid, str(tmp_path / "invalid")]) == 1
    assert not (tmp_path / "invalid").exists()
    assert page.read_text() == "working tree edit"

def test_monorepo_builds_only_the_selected_application(tmp_path):
    repo = tmp_path / "repository"
    selected = repo / "apps" / "garden"
    selected.mkdir(parents=True)
    (selected / "index.html").write_text("garden")
    other = repo / "apps" / "sketchbook"
    other.mkdir()
    (other / "shared-library").symlink_to("../shared")
    (repo / "LICENSE").write_text("fixture license")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git_output(repo, "add", ".")
    git_output(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixtures")
    output = tmp_path / "garden"
    assert main(["app", "package", "--source", str(repo), "--application", "garden", "--app-dir", "apps/garden",
                 "--revision", git_output(repo, "rev-parse", "HEAD"), "--output", str(output), "--platform", "linux-x64"]) == 0
    archive = (output / "application.run").read_bytes().split(b"\n__APPLICATION_PAYLOAD__\n", 1)[1]
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        assert tar.extractfile("LICENSE").read() == b"fixture license"
        assert set(tar.getnames()) == {"LICENSE", "install.json", "site", "site/index.html"}
