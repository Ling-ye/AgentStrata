"""Generation requests and provider ports; no IO."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import re
from typing import Callable, Protocol

MAX_UPLOAD = 48 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_FILES = 256
MAX_PIXELS = 4096 * 4096
INPUT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".psd", ".zip"}
TERMINAL = {"completed", "failed", "cancelled"}


class GenerationError(ValueError):
    """A public, credential-free diagnostic suitable for the workbench."""

    def __init__(self, message: str, *, code: str | None = None, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class Cancelled(GenerationError):
    pass


@dataclass(frozen=True)
class GenerationRequest:
    name: str
    prompt: str = ""
    information: dict[str, str] = field(default_factory=dict)
    # Image-only preserves the image. Explicit redraw applies prompt/info via image edits.
    redraw: bool = False

    @classmethod
    def parse(cls, value: dict) -> GenerationRequest:
        if not isinstance(value, dict) or set(value) - {"name", "prompt", "information", "redraw"}:
            raise GenerationError("请求仅支持 name、prompt、information 和 redraw")
        name, prompt, info = value.get("name", ""), value.get("prompt", ""), value.get("information", {})
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise GenerationError("角色名称须为 1–80 个字符")
        if not isinstance(prompt, str) or len(prompt) > 12000:
            raise GenerationError("提示词须为不超过 12000 字符的文本")
        if (not isinstance(info, dict) or len(info) > 32 or any(
                not isinstance(k, str) or not isinstance(v, str) or len(k) > 80 or len(v) > 2000
                for k, v in info.items()) or len(json.dumps(info)) > 16000):
            raise GenerationError("角色资料须为文本键值表，最多 32 项、16000 字符")
        if type(value.get("redraw", False)) is not bool:
            raise GenerationError("redraw 须为布尔值")
        return cls(name.strip(), prompt.strip(), info, value.get("redraw", False))

    def image_prompt(self) -> str:
        details = "\n".join(f"{key}: {value}" for key, value in self.information.items())
        return (
            "Create a single front-facing anime character illustration for Live2D rigging. "
            "Full body, neutral pose, arms slightly away from torso, visible hands and feet, "
            "readable facial features, coherent costume, plain white background, no text or watermark. "
            "Use any supplied image as a character reference.\n"
            f"Character: {self.name}\nUser description:\n{self.prompt}\nCharacter information:\n{details}"
        )


def job_id(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise GenerationError("无效的任务标识")
    return value


class ImageProvider(Protocol):
    def generate(self, prompt: str, reference: Path | None, destination: Path) -> None: ...


class LayerProvider(Protocol):
    def decompose(self, image: Path, destination: Path, check: Callable[[], None]) -> None: ...


class RigProvider(Protocol):
    def build(self, source: Path, destination: Path, work: Path) -> dict: ...
