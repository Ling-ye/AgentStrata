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


def space_failure(exc: Exception, *, authenticated: bool, key_env: str,
                  connected: bool = True) -> GenerationError:
    """Project known service failures without exposing remote HTML, URLs or credentials."""
    message = str(exc)
    lower = message.lower()
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if "quota" in lower and ("gpu" in lower or "exceeded" in lower):
        amounts = re.search(r"(\d{1,8}(?:\.\d{1,3})?)s requested vs\.?\s*(\d{1,8}(?:\.\d{1,3})?)s left", message, re.I)
        delay = re.search(r"try again in\s+(\d{1,3}):([0-5]\d):([0-5]\d)", message, re.I)
        seconds = sum(int(part) * scale for part, scale in zip(delay.groups(), (3600, 60, 1))) if delay else None
        detail = f"（本次申请 {amounts[1]} 秒，当前剩余 {amounts[2]} 秒）" if amounts else ""
        action = ("当前为匿名调用；可在生成器 config/runtime.env 配置 " + key_env +
                  " 后重启，使用自己的 Hugging Face 账户配额。") if not authenticated else (
                  "已配置 Hugging Face 凭据，请检查该账户的可用额度。")
        return GenerationError("See-through Space 的 ZeroGPU 配额不足" + detail + "。" + action +
            "也可按服务建议稍后手动重试；本次请求不会自动重复提交。",
            code="space_quota_exhausted", retry_after_seconds=seconds)
    if type(exc).__name__ == "AuthenticationError" or status in {401, 403}:
        action = "凭据无效或没有该 Space 的访问权限" if authenticated else "尚未配置 Hugging Face 访问凭据"
        return GenerationError(f"See-through Space 认证失败：{action}；检查 {key_env} 后重启生成器。",
                               code="space_auth_failed")
    if status == 429:
        return GenerationError("See-through Space 请求被限流，请稍后手动重试；不会自动重复提交。",
                               code="space_rate_limited")
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        if not connected:
            return GenerationError("连接 See-through Space 时超时；尚未上传图片或提交拆层任务。",
                                   code="space_timeout")
        return GenerationError("See-through Space 请求超时；远端任务可能仍在执行，请先确认服务状态再重试。",
                               code="space_timeout")
    if isinstance(exc, httpx.NetworkError) or status in {404, 502, 503, 504}:
        return GenerationError("无法连接 See-through Space，或服务尚未就绪；请检查配置的 Space 页面后重试。",
                               code="space_unavailable")
    if "api_name" in lower or type(exc).__name__ == "SerializationSetupError":
        return GenerationError("See-through Space 的 /inference 接口不匹配；请检查服务是否仍使用约定的拆层接口。",
                               code="space_protocol_error")
    return GenerationError("See-through Space 远端执行失败；服务未提供可安全展示的具体原因，请检查 Space 页面。",
                           code="space_execution_failed")


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
                            download_files=False, analytics_enabled=False,
                            httpx_kwargs={"timeout": self.config.timeout_seconds})
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
            if (not isinstance(result, (list, tuple)) or not result or not isinstance(result[0], dict)
                    or not isinstance(result[0].get("url"), str)):
                raise GenerationError("See-through Space 没有返回有效的 PSD 下载地址；请检查拆层接口。",
                                      code="space_protocol_error")
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
        except Exception as exc:
            raise space_failure(exc, authenticated=bool(token), key_env=self.config.decompose_key_env,
                                connected=client is not None) from None
        finally:
            if client is not None:
                client.close()
