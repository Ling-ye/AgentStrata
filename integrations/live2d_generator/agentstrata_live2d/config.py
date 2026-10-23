"""Operator configuration; services cannot be chosen by untrusted job inputs."""
from __future__ import annotations

from dataclasses import dataclass, fields
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .types import GenerationError


def endpoint(value: str) -> str:
    if not value:
        return ""
    url = urlsplit(value)
    if (url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(c in value for c in "\r\n\0")
            or (url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"})):
        raise GenerationError("服务地址须为无凭据的 HTTPS URL，或本机 HTTP URL")
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    image_base_url: str = ""
    image_model: str = ""
    image_key_env: str = "AGENTSTRATA_LIVE2D_IMAGE_KEY"
    decompose_url: str = ""
    decompose_space: str = ""
    decompose_key_env: str = "AGENTSTRATA_LIVE2D_DECOMPOSE_KEY"
    resolution: int = 768
    timeout_seconds: int = 1800
    port: int = 12394

    @classmethod
    def load(cls, path: Path) -> Config:
        return cls.parse(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def parse(cls, value: dict) -> Config:
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise GenerationError("生成器配置包含未知字段")
        result = cls(**value)
        for name in ("image_base_url", "image_model", "image_key_env", "decompose_url",
                     "decompose_space", "decompose_key_env"):
            if not isinstance(getattr(result, name), str):
                raise GenerationError("服务配置字段须为字符串")
        endpoint(result.image_base_url)
        endpoint(result.decompose_url)
        if bool(result.image_base_url) != bool(result.image_model):
            raise GenerationError("image_base_url 和 image_model 必须一起配置")
        if result.decompose_url and result.decompose_space:
            raise GenerationError("只能配置一个拆层服务")
        if result.decompose_space and not re.fullmatch(r"[\w.-]+/[\w.-]+", result.decompose_space):
            raise GenerationError("decompose_space 使用 owner/space 格式")
        for name in (result.image_key_env, result.decompose_key_env):
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
                raise GenerationError("凭据配置必须是环境变量名称")
        for name, lower, upper in (("port", 1, 65535), ("resolution", 512, 1280),
                                   ("timeout_seconds", 30, 7200)):
            if type(getattr(result, name)) is not int or not lower <= getattr(result, name) <= upper:
                raise GenerationError(f"{name} 必须在 {lower}–{upper} 范围内")
        return result

    def capabilities(self) -> dict:
        return {"text": bool(self.image_base_url), "image": bool(self.decompose_url or self.decompose_space),
                "layers": True, "image_model": self.image_model,
                "image_service": self.image_base_url,
                "decompose_service": self.decompose_url or self.decompose_space,
                "image_key_configured": bool(os.environ.get(self.image_key_env)),
                "timeout_seconds": self.timeout_seconds}


def load_environment(path: Path) -> None:
    """Read our quoted EnvironmentFile subset without shell evaluation."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise GenerationError("环境文件格式无效")
        os.environ[key] = json.loads(value) if value.startswith('"') else value


def source_notice() -> dict:
    """The same deployment source variable used by the project's other network interfaces."""
    source = os.environ.get("CHATCOPILOT_SOURCE_URL", "")
    try:
        url = urlsplit(source)
        configured = bool(source and url.scheme in {"http", "https"} and url.hostname
                          and not url.username and not url.password and not url.query)
    except ValueError:
        configured = False
    return {"license": "AGPL-3.0-only", "repository": "https://github.com/Ling-ye/AgentStrata",
            "source_url": source if configured else "", "source_configured": configured,
            "notice": "自有插件采用 AGPL-3.0-only；第三方程序、模型和形象保留各自条款。"
                      + ("对应源码地址已配置，可用性与完整性须独立核对。" if configured else
                         "运行版本的对应源码地址尚未配置，不能据此通过对外部署验收。")}
