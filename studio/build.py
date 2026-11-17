"""Build the local Studio installer or the source-build reading sample on Windows + WSL."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

from chatcopilot.core.source_snapshot import source_manifest
from studio.delivery import native_inputs, pack, windows_path


def studio_archive(root: Path, archive: Path) -> None:
    """Package current source with frontend resources compiled from that source."""
    if archive.exists():
        raise FileExistsError(archive)
    manifest = source_manifest(root)
    with tempfile.TemporaryDirectory(prefix="studio-web-") as temp:
        frontend = Path(temp) / "dist"
        subprocess.run(["npm", "run", "build", "--prefix", str(root / "console/web"),
                        "--", "--outDir", str(frontend)], check=True)
        assets = sorted(frontend.rglob("*"))
        if not (frontend / "index.html").is_file() or any(p.is_symlink() for p in assets):
            raise ValueError("Studio frontend build must contain index.html and regular resources")
        if source_manifest(root) != manifest:
            raise ValueError("Studio source changed while building frontend resources")
        with tarfile.open(archive, "x:gz") as stream:
            for name in manifest:
                stream.add(root / name, arcname=name, recursive=False)
            for path in assets:
                if path.is_file():
                    stream.add(path, arcname="console/web/dist/" + path.relative_to(frontend).as_posix(), recursive=False)
    if source_manifest(root) != manifest:
        archive.unlink()
        raise ValueError("Studio source changed while preparing the installer archive")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("studio", "reader"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--windows-build")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    stage = args.output.absolute()
    if args.kind == "reader":
        from studio.projects import project_inputs, source_tree
        with tempfile.TemporaryDirectory() as temp:
            sample = source_tree(Path(temp), "reader", root, native_inputs(project_inputs(root), "desktop-reader"))
            pack(sample, stage, bot_relative="bots/desktop-reader/bot.yaml", name="读书助手",
                 install_id="desktop-reader", windows_build=args.windows_build)
        return
    stage.mkdir(parents=True, exist_ok=False)
    archive = stage / "framework.tar.gz"
    studio_archive(root, archive)
    with archive.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    (stage / "framework.sha256").write_text(sha + "\n")
    (stage / "desktop-build.json").write_text(json.dumps({"installId":"agentstrata-studio", "name":"AgentStrata 开发工作台",
                                                         "kind":"studio","botId":"studio-guide"}))
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
               windows_path(root / "integrations/desktop_app/build.ps1"), "-Studio", "-Stage", windows_path(stage)]
    if args.windows_build:
        command.extend(["-BuildRoot", args.windows_build])
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
