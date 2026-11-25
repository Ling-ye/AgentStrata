"""Prepare a pinned, separate Windows GPT-SoVITS runtime for this plugin."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import zipfile

from chatcopilot.core.install_artifacts import download, extract_archive, sha256

from .preset_files import staging
from .repository import private_directory, relative_file, write_json
from .types import GenerationError


def _archive(item: dict, path: Path, destination: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        prefix = ""
        if item.get("member"):
            matches = [member.filename for member in members
                       if Path(member.filename).name.casefold() == item["member"].casefold()]
            if len(matches) != 1:
                raise GenerationError("中文发音模型的固定归档格式不符")
            prefix = matches[0][:-len(item["member"])]
        elif members and all(member.filename.startswith("nltk_data/") for member in members):
            prefix = "nltk_data/"
        for member in members:
            if not member.filename.startswith(prefix) or member.filename.startswith("__MACOSX/"):
                continue
            target = relative_file(destination, member.filename[len(prefix):])
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with archive.open(member) as data, target.open("xb") as output:
                shutil.copyfileobj(data, output)


def prepare_assets(runtime: Path, cache: Path, manifest: Path) -> Path:
    manifest_digest = sha256(manifest)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    upstream = runtime / "upstream"
    marker = upstream / ".agentstrata-voice-pin"
    if marker.is_file() and marker.read_text(encoding="utf-8") == manifest_digest:
        for item in document["model"]:
            if sha256(relative_file(upstream, item["path"])) != item["sha256"]:
                raise GenerationError("GPT-SoVITS 权重或基础资源损坏")
        return upstream
    if upstream.exists():
        raise GenerationError("已存在其他版本的语音运行目录，请准备独立安装")
    with staging(upstream) as target:
        pin = document["source"]
        extract_archive(download(pin["url"], pin["sha256"], cache), target)
        for item in document["model"]:
            destination = relative_file(target, item["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(download(item["url"], item["sha256"], cache), destination)
        for item in document["archives"]:
            _archive(item, download(item["url"], item["sha256"], cache), relative_file(target, item["path"]))
        # The Windows runtime uses the portable implementation of the same tokenizer API.
        for filename in ("chinese.py", "chinese2.py", "tone_sandhi.py"):
            text = target / "GPT_SoVITS/text" / filename
            text.write_text(text.read_text(encoding="utf-8").replace("jieba_fast", "jieba"), encoding="utf-8")
        # Dataset and TTS helpers use this module without starting the upstream Web UI.
        audio = target / "tools/my_utils.py"
        source = audio.read_text(encoding="utf-8").replace("import gradio as gr\n", "")
        for definition in (
            "def check_for_existance(file_list: list = None, is_train=False, is_dataset_processing=False):",
            "def check_details(path_list=None, is_train=False, is_dataset_processing=False):",
        ):
            source = source.replace(definition + "\n", definition + "\n    import gradio as gr\n\n")
        audio.write_text(source, encoding="utf-8")
        (target / marker.name).write_text(manifest_digest, encoding="utf-8")
    return upstream


def configure_voice(plugin: Path, application: Path, port: int, *, cache: Path | None = None) -> Path:
    from importlib.resources import files
    runtime = private_directory(plugin / "runtime")
    python = runtime / "voice-venv/Scripts/python.exe"
    if not python.is_file():
        raise GenerationError("安装器尚未准备独立 Python 3.10 语音环境")
    pin = Path(str(files(__package__).joinpath("resources/gpt-sovits.lock.json")))
    upstream = prepare_assets(runtime, cache or runtime / "downloads", pin)
    vtuber = application / ".agentstrata-data/plugins/open-llm-vtuber"
    ffmpeg = vtuber / "runtime/tools/ffmpeg/bin/ffmpeg.exe"
    sensevoice = vtuber / "data/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    if not ffmpeg.is_file() or not (sensevoice / "model.int8.onnx").is_file():
        raise GenerationError("桌宠安装缺少 FFmpeg 或 SenseVoice 基础资源")
    config = plugin / "config/voice.json"
    write_json(config, {"python": str(python), "runtime": str(runtime), "upstream": str(upstream),
        "ffmpeg": str(ffmpeg), "sensevoice": str(sensevoice), "library": str(plugin / "data/characters"), "port": port})
    return config
