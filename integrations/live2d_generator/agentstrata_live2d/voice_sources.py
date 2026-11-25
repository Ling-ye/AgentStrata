"""Read authorized voice inputs without giving the caller local paths or commands."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import html
import http.client
from html.parser import HTMLParser
import ipaddress
from pathlib import Path
import socket
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from .preset_files import copy_file, digest, read_json
from .repository import private_directory, relative_file, restrict, write_json
from .types import GenerationError, MAX_UPLOAD, job_id

LANGUAGES = {"zh", "ja", "en", "ko", "yue"}
FORMATS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".opus", ".webm"}
LICENSES = {
    "https://creativecommons.org/publicdomain/zero/1.0/",
    "https://creativecommons.org/licenses/by/4.0/",
}


@dataclass(frozen=True)
class VoiceSource:
    artist: str
    title: str
    kind: str
    language: str
    page: str = ""
    audio: str = ""
    license: str = ""
    upload_id: str = ""

    @classmethod
    def parse(cls, data: dict) -> VoiceSource:
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise GenerationError("音源字段无效")
        if any(not isinstance(v, str) or len(v) > 2048 for v in data.values()):
            raise GenerationError("音源字段必须是有界文本")
        try:
            result = cls(**{k: html.unescape(v).strip() for k, v in data.items()})
        except TypeError:
            raise GenerationError("音源需填写艺人、作品、类型与语言") from None
        if not result.artist or not result.title or result.kind not in {"speech", "song"} or result.language not in LANGUAGES:
            raise GenerationError("音源需填写艺人、作品、口播或歌曲，以及支持的语言")
        if result.upload_id:
            job_id(result.upload_id)
        for value in (result.page, result.audio):
            if value:
                _https(value)
        return result


def _https(url: str):
    try:
        value = urlsplit(url)
        if (value.scheme != "https" or not value.hostname or value.port not in {None, 443}
                or value.username is not None or value.password is not None
                or any(c.isspace() or c == "\\" for c in url)):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise GenerationError("音源必须是公开的 HTTPS 地址") from None


def _public_file(url: str, target: Path, *, limit: int, media: tuple[str, ...]) -> None:
    parsed = _https(url)
    host = parsed.hostname
    address = None
    connection = None
    try:
        for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
            current = ipaddress.ip_address(info[4][0])
            if not current.is_global or current.is_multicast:
                raise GenerationError("音源地址不能解析到私有地址")
            address = str(current)
        if address is None:
            raise GenerationError("音源地址无法解析")
        connection = http.client.HTTPSConnection(host, 443, timeout=20)
        # Use the vetted IP for transport and the publisher hostname for TLS/SNI.
        connection._create_connection = lambda addr, timeout, source_address: socket.create_connection(
            (address, addr[1]), timeout, source_address)
        query = "?" + parsed.query if parsed.query else ""
        connection.request("GET", (parsed.path or "/") + query, headers={"User-Agent": "AgentStrata Character Studio"})
        response = connection.getresponse()
        if response.status != 200 or not response.headers.get("Content-Type", "").lower().startswith(media):
            raise GenerationError("发布页没有直接提供所声明的音源")
        data = response.read(limit + 1)
        if not data or len(data) > limit:
            raise GenerationError("音源为空或超过上传大小限制")
        with target.open("xb") as output:
            output.write(data)
        restrict(target)
    except (OSError, http.client.HTTPException, UnicodeError):
        raise GenerationError("公开音源暂时无法读取") from None
    finally:
        if connection is not None:
            connection.close()


class _Sources(HTMLParser):
    def __init__(self, page: str):
        super().__init__(convert_charrefs=True)
        self.page = page
        self.license = set()
        self.audio = set()

    def handle_starttag(self, tag: str, attrs):
        values = dict(attrs)
        if "license" in (values.get("rel") or "").lower().split() and values.get("href"):
            self.license.add(urljoin(self.page, values["href"]))
        if tag in {"audio", "source"} and values.get("src"):
            self.audio.add(urljoin(self.page, values["src"]))
        if tag == "meta" and values.get("property") in {"og:audio", "og:audio:secure_url"} and values.get("content"):
            self.audio.add(urljoin(self.page, values["content"]))


def upload_audio(library: Path, original: Path, details: dict, permission: str) -> dict:
    source = VoiceSource.parse(details)
    if not isinstance(permission, str) or not permission.strip() or len(permission) > 2048:
        raise GenerationError("请填写这份录音用于训练的依据")
    filename = "audio" + original.suffix.lower()
    if original.suffix.lower() not in FORMATS or original.stat().st_size > MAX_UPLOAD:
        raise GenerationError("支持 WAV、MP3、M4A、OGG、FLAC 或 Opus 音频")
    identifier = uuid4().hex
    root = private_directory(relative_file(library, ".audio/" + identifier))
    copy_file(original, root / filename)
    write_json(root / "source.json", asdict(source) | {
        "file": filename, "permission": permission.strip(), "upload_id": identifier,
    })
    return {"upload_id": identifier, "artist": source.artist, "title": source.title,
            "language": source.language, "kind": source.kind}


def materialize(library: Path, sources: list[dict], work: Path) -> list[dict]:
    accepted = []
    remaining = MAX_UPLOAD
    for index, data in enumerate(sources):
        source = VoiceSource.parse(data)
        if source.upload_id:
            root = relative_file(library, ".audio/" + source.upload_id)
            record = read_json(root / "source.json")
            if any(record[k] != getattr(source, k) for k in ("artist", "title", "kind", "language")):
                raise GenerationError("音源登记与制作请求不匹配")
            stored = relative_file(root, record["file"])
            size = stored.stat().st_size
            if size > remaining:
                raise GenerationError("录音合计超过上传大小限制")
            target = work / f"source-{index}{stored.suffix}"
            copy_file(stored, target)
            evidence = record["permission"]
            source = VoiceSource.parse({key: record[key] for key in VoiceSource.__dataclass_fields__})
        elif source.page and source.audio and source.license in LICENSES:
            suffix = Path(_https(source.audio).path).suffix.lower()
            if suffix not in FORMATS:
                continue
            page = work / f"source-{index}.html"
            try:
                _public_file(source.page, page, limit=2 * 1024 * 1024, media=("text/html",))
                metadata = _Sources(source.page)
                metadata.feed(page.read_text(encoding="utf-8", errors="replace"))
                if source.license not in metadata.license or source.audio not in metadata.audio:
                    continue
                target = work / f"source-{index}{suffix}"
                _public_file(source.audio, target, limit=remaining, media=("audio/", "video/webm"))
                size = target.stat().st_size
                evidence = source.license
            except GenerationError:
                continue
            finally:
                page.unlink(missing_ok=True)
        else:
            continue
        remaining -= size
        accepted.append(asdict(source) | {"file": target.name, "sha256": digest(target), "permission": evidence})
    if not accepted:
        raise GenerationError("缺少可直接获取且有训练依据的录音。可保存已找到的候选，或上传已获准的片段。",
                              code="voice_input_required")
    if len({(m["artist"], m["language"]) for m in accepted}) != 1:
        raise GenerationError("一次训练应使用同一说话人和同一素材语言", code="voice_input_required")
    return accepted
