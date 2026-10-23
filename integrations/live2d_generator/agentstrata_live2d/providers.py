"""Explicit image and decomposition transports. Never retry a submitted generation."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .config import Config
from .repository import image_info
from .types import GenerationError, MAX_UPLOAD


def response_bytes(client: httpx.Client, method: str, url: str, *, limit: int = MAX_UPLOAD, **kwargs) -> bytes:
    try:
        with client.stream(method, url, **kwargs) as response:
            if response.status_code >= 300:
                raise GenerationError(f"服务请求失败（HTTP {response.status_code}）；检查配置、额度或服务状态")
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise GenerationError("服务返回的数据超过体积限制")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError:
        raise GenerationError("服务连接中断或超时；该请求不会自动重试") from None


class ImagesAPI:
    def __init__(self, config: Config):
        self.config = config

    def generate(self, prompt: str, reference: Path | None, destination: Path) -> None:
        config = self.config
        if not config.image_base_url:
            raise GenerationError("尚未配置绘图服务；请配置 image_base_url 和 image_model，或上传立绘/分层素材")
        token = os.environ.get(config.image_key_env, "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = {"model": config.image_model, "prompt": prompt, "n": 1, "size": "1024x1536"}
        with httpx.Client(headers=headers, timeout=min(config.timeout_seconds, 600),
                          follow_redirects=False) as client:
            base = config.image_base_url.rstrip("/")
            if reference is None:
                body = response_bytes(client, "POST", base + "/images/generations",
                                      limit=MAX_UPLOAD * 2, json=data)
            else:
                image_info(reference)
                with reference.open("rb") as image:
                    body = response_bytes(client, "POST", base + "/images/edits", limit=MAX_UPLOAD * 2,
                                          data={k: str(v) for k, v in data.items()},
                                          files={"image": (reference.name, image)})
        try:
            encoded = json.loads(body)["data"][0]["b64_json"]
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, KeyError, IndexError, TypeError):
            raise GenerationError("绘图服务必须返回 data[0].b64_json 图像数据") from None
        if len(payload) > MAX_UPLOAD:
            raise GenerationError("生成图片超过 48 MiB")
        destination.write_bytes(payload)
        image_info(destination)


class SeeThroughHTTP:
    def __init__(self, config: Config):
        self.config = config

    def decompose(self, image: Path, destination: Path, check: Callable[[], None]) -> None:
        base = self.config.decompose_url.rstrip("/")
        if not base:
            raise GenerationError("尚未配置拆层服务；请配置 decompose_url / decompose_space，或上传 PSD/图层 ZIP")
        token = os.environ.get(self.config.decompose_key_env, "")
        headers = {"X-Auth-Token": token} if token else {}
        with httpx.Client(headers=headers, timeout=60, follow_redirects=False) as client:
            body = response_bytes(client, "POST", base + "/decompose", content=image.read_bytes(),
                                  headers={"Content-Type": "application/octet-stream"}, limit=16384)
            try:
                identifier = json.loads(body)["job_id"]
                if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identifier):
                    raise ValueError
                while True:
                    check()
                    state = json.loads(response_bytes(client, "GET", f"{base}/jobs/{identifier}", limit=16384))
                    if state["status"] == "done":
                        break
                    if state["status"] == "error":
                        raise GenerationError("拆层服务执行失败；请查看服务端诊断")
                    if state["status"] not in {"queued", "running"}:
                        raise GenerationError("拆层服务返回未知状态")
                    time.sleep(2)
                check()
                destination.write_bytes(response_bytes(client, "GET", f"{base}/jobs/{identifier}/result"))
            except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                if isinstance(exc, GenerationError):
                    raise
                raise GenerationError("拆层服务返回无效协议数据") from None


class SeeThroughSpace:
    def __init__(self, config: Config):
        self.config = config

    def decompose(self, image: Path, destination: Path, check: Callable[[], None]) -> None:
        from gradio_client import Client, handle_file

        # The configured Space is the only remote target. Do not inherit a cached HF identity.
        token = os.environ.get(self.config.decompose_key_env) or False
        client = None
        try:
            client = Client(self.config.decompose_space, token=token, verbose=False,
                            download_files=False, analytics_enabled=False)
            job = client.submit(handle_file(str(image)), self.config.resolution, 42, True,
                                api_name="/inference")
            try:
                while not job.done():
                    check()
                    time.sleep(1)
                result = job.result()
            except BaseException:
                job.cancel()
                raise
            url = result[0]["url"]
            target, origin = urlsplit(url), urlsplit(client.src)
            if (target.scheme != "https" or target.netloc != origin.netloc
                    or target.username or target.password):
                raise GenerationError("拆层结果必须来自配置的 Space 同源地址")
            check()
            # Do not let Gradio download an unbounded gallery into a shared /tmp directory.
            with httpx.Client(headers=client.headers, timeout=120, follow_redirects=False) as http:
                destination.write_bytes(response_bytes(http, "GET", url))
        except GenerationError:
            raise
        except Exception:
            raise GenerationError("See-through Space 调用失败；检查服务可用性、GPU 配额及凭据") from None
        finally:
            if client is not None:
                client.close()
