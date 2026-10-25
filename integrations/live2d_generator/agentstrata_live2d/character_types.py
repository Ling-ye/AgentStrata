"""Portable character contracts and the catalog download port; no IO."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Protocol

from .types import GenerationError, MAX_UPLOAD

PRESET_FORMAT = "agentstrata-character-preset-v1"
CATALOG_FORMAT = "agentstrata-character-catalog-v1"
BACKUP_FORMAT = "agentstrata-character-backup-v1"
REVISION_FORMAT = "agentstrata-character-revision-v1"
PRESET_FILES = {"profile.json", "avatar.zip", "preview.png"}
MODEL_SUFFIXES = {".json", ".moc3", ".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3", ".txt", ".md"}


def character_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise GenerationError("角色 ID 无效")
    return value


def revision_number(value: int) -> int:
    if type(value) is not int or value < 1:
        raise GenerationError("角色版本须为正整数")
    return value


def profile(value: dict) -> dict:
    defaults = {"name": "", "description": "", "identity": "", "response_style": "",
                "author": "", "license": "", "source": "",
                "voice": {"provider": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"}}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise GenerationError("角色资料包含未知字段")
    result = defaults | value
    for key, item in result.items():
        if key != "voice" and (not isinstance(item, str) or len(item) > (80 if key == "name" else 12000)):
            raise GenerationError("角色资料须为文本，名称最多 80 字，其余字段最多 12000 字")
    if not result["name"].strip():
        raise GenerationError("角色名称不能为空")
    voice = result["voice"]
    if (not isinstance(voice, dict) or set(voice) != {"provider", "voice"}
            or voice["provider"] != "edge_tts" or not isinstance(voice["voice"], str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,120}", voice["voice"])):
        raise GenerationError("声音首版支持 Edge TTS 音色 ID，不接受端点或凭据")
    return result


def catalog(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != {"format", "entries"}
            or value["format"] != CATALOG_FORMAT or not isinstance(value["entries"], list)):
        raise GenerationError("预设目录格式无效")
    seen = set()
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
                "id", "version", "name", "description", "author", "license", "source",
                "asset_id", "size", "sha256", "preview_asset_id", "preview_size", "preview_sha256"}:
            raise GenerationError("预设目录条目字段无效")
        key = (character_id(entry["id"]), revision_number(entry["version"]))
        if key in seen:
            raise GenerationError("预设目录包含重复版本")
        seen.add(key)
        profile({k: entry[k] for k in ("name", "description", "author", "license", "source")})
        for prefix in ("", "preview_"):
            if (type(entry[prefix + "asset_id"]) is not int or entry[prefix + "asset_id"] < 1
                    or type(entry[prefix + "size"]) is not int
                    or not 0 < entry[prefix + "size"] <= MAX_UPLOAD
                    or not isinstance(entry[prefix + "sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", entry[prefix + "sha256"])):
                raise GenerationError("预设目录资产标识、大小或摘要无效")
    return value


class PresetSource(Protocol):
    def catalog(self) -> dict: ...
    def download(self, asset_id: int, destination: Path, *, size: int, sha256: str) -> None: ...
