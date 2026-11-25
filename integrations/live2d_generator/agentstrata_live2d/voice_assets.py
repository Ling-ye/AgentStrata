"""Immutable, content-addressed voice assets in the existing private character library."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import wave

from .preset_files import copy_tree, inventory, read_json, verify_inventory
from .repository import relative_file, write_json
from .types import GenerationError

FILES = {"gpt.safetensors", "sovits.safetensors", "weights.json", "reference.wav",
         "preview.wav", "preview-2.wav", "voice.json"}
VOICE_FORMAT = "agentstrata-character-voice-v1"


def voice_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise GenerationError("语音资产标识无效")
    return value


def wav_info(path: Path) -> dict:
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            if not frames or rate <= 0 or not wav.readframes(frames):
                raise ValueError
            return {"sample_rate": rate, "frames": frames}
    except (OSError, EOFError, ValueError, wave.Error):
        raise GenerationError("语音不是可播放的 PCM WAV") from None


def _manifest(root: Path) -> dict:
    record = read_json(relative_file(root, "manifest.json"))
    if (set(record) != {"format", "files"} or record["format"] != VOICE_FORMAT
            or not isinstance(record["files"], dict) or set(record["files"]) != FILES):
        raise GenerationError("语音资产文件清单无效")
    verify_inventory(root, record["files"], omit={"manifest.json"})
    return record


def _id(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolve_voice(library: Path, identifier: str) -> Path:
    root = relative_file(library, ".voices/" + voice_id(identifier))
    if _id(_manifest(root)) != identifier:
        raise GenerationError("语音资产摘要不匹配")
    return root


def publish_voice(library: Path, source: Path) -> str:
    from .preset_files import staging

    members = inventory(source, omit={"manifest.json"})
    if set(members) != FILES:
        raise GenerationError("训练没有生成完整的语音资产")
    if any(not value["size"] for value in members.values()):
        raise GenerationError("语音资产包含空文件")
    for name in ("reference.wav", "preview.wav", "preview-2.wav"):
        wav_info(relative_file(source, name))
    record = {"format": VOICE_FORMAT, "files": members}
    write_json(source / "manifest.json", record)
    identifier = _id(record)
    target = relative_file(library, ".voices/" + identifier)
    if target.exists():
        resolve_voice(library, identifier)
    else:
        with staging(target) as temporary:
            copy_tree(source, temporary)
    return identifier


def voice_reference(details: dict) -> str | None:
    voice = details["voice"]
    return voice_id(voice["voice"]) if voice["provider"] == "gpt_sovits" else None
