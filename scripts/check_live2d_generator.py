#!/usr/bin/env python3
"""Install the generator wheel into Python 3.12 and build a real model from synthetic layers.

No image API, GPU, model credentials or public demo calls. SDK loading remains a separate check.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def probe(request: dict) -> None:
    from agentstrata_live2d.config import Config
    from agentstrata_live2d.manage import prepare, check
    from agentstrata_live2d.runtime import JobRuntime
    from agentstrata_live2d.types import GenerationRequest, TERMINAL
    prepared = prepare(request)
    from image2live2d.samples import make_sample_layers

    assert not prepared["observable_agents"]
    assert check(request)["ok"]
    root = Path(request["root"])
    layers = make_sample_layers(root / "data/sample-layers")
    source = root / "data/sample.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for path in layers.glob("*.png"):
            archive.write(path, path.name)
    with JobRuntime(root / "data/jobs", Config.load(root / "config/generator.json")) as runtime:
        state = runtime.start(GenerationRequest("Synthetic rig acceptance"), source)
        while state["status"] not in TERMINAL:
            time.sleep(0.2)
            state = runtime.get(state["id"])
        assert state["status"] == "completed", state
        with zipfile.ZipFile(runtime.artifact(state["id"], "model.zip")) as archive:
            assert archive.read("avatar.moc3").startswith(b"MOC3")
            model = json.loads(archive.read("avatar.model3.json"))
            assert model["FileReferences"]["Motions"]["Idle"]
            assert model["FileReferences"]["Expressions"]
        assert state.get("character_version") == 1, state
        characters = runtime.characters
        role = characters.get(state["character_id"])
        assert role["draft_jobs"] == [state["id"]]
        details = role["draft"] | {"author": "Example", "license": "Synthetic acceptance fixture only",
                                  "source": "image2live2d sample layers"}
        characters.save_draft(role["id"], details)
        second = characters.save_version(role["id"], 1)
        assert second["version"] == 2
        preset = root / "data/acceptance-preset.zip"
        characters.export(role["id"], 2, preset)
        backup = root / "data/acceptance-backup"
        characters.backup(backup)
        from agentstrata_live2d.runtime import character_services
        restored, _ = character_services(root / "data/restored/jobs")
        assert restored.restore(backup)["restored"] == [role["id"]]
        assert restored.get(role["id"])["versions"] == [1, 2]
        imported, _ = character_services(root / "data/imported/jobs")
        assert imported.import_preset(preset)["version"] == 2
        assert imported.get(role["id"])["read_only"]
        forbidden = {"chatcopilot.agent", "chatcopilot.gateway", "agentstrata_vtuber"}
        assert not any(module in sys.modules for module in forbidden)
    print("Installed generator: real synthetic moc3, character revisions, preset and backup roundtrip verified; no SDK or image-service claim")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    if args.probe:
        probe(json.loads(args.probe.read_text()))
        return 0
    if sys.version_info[:2] != (3, 12):
        parser.error("Run this installed-plugin check with Python 3.12")
    sys.path.insert(0, str(ROOT / "src"))
    from chatcopilot.core.install_artifacts import dependency_environment, write_private
    from chatcopilot.plugins.catalog import PLUGINS
    from chatcopilot.plugins.runtime import bootstrap, ensure_uv
    from plugin_artifacts import build

    with tempfile.TemporaryDirectory(prefix="as-live2d-compat-") as temporary:
        work = (args.work_dir or Path(temporary)).resolve()
        work.mkdir(parents=True, exist_ok=True)
        artifacts = work / "artifacts"
        artifacts.mkdir(exist_ok=True)
        subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(artifacts)],
                       cwd=ROOT, stdout=subprocess.DEVNULL, check=True)
        wheel, _ = build(ROOT / "integrations/live2d_generator", artifacts)
        host = next(artifacts.glob("agentstrata-*.whl"))
        uv = ensure_uv(work)
        root = work / "installation"
        python = bootstrap(PLUGINS["live2d-generator"], root, (host, wheel), uv)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        request = {"root": str(root), "uv": str(uv), "options": {"port": port},
                   "host": {"source_url": "https://example.com/source"}}
        request_file = root / "config/request.json"
        write_private(request_file, json.dumps(request))
        copied = work / "probe.py"
        shutil.copyfile(__file__, copied)
        subprocess.run([str(python), str(copied), "--probe", str(request_file)], cwd=work,
                       env=dependency_environment(), check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
