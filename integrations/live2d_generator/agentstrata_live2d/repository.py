"""Private job files and bounded model/layer archives."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from uuid import uuid4
import zipfile

from .types import GenerationError, MAX_EXPANDED, MAX_FILES, MAX_PIXELS, MAX_UPLOAD, job_id


def private_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.resolve() != path:
        raise GenerationError("工作目录不得包含符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise GenerationError("工作目录必须属于当前用户")
    path.chmod(0o700)
    return path


def write_json(path: Path, value: dict | list) -> None:
    write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def write_bytes(path: Path, value: bytes) -> None:
    private_directory(path.parent)
    if path.is_symlink():
        raise GenerationError("拒绝写入符号链接")
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(value)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def relative_file(root: Path, name: str) -> Path:
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name or "%" in name
            or "?" in name or "#" in name or "\0" in name):
        raise GenerationError("资产引用必须是普通相对路径")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(p in {"..", ".", ""} for p in name.split("/")):
        raise GenerationError("资产路径越界")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or any(
            (root / Path(*relative.parts[:n])).is_symlink() for n in range(1, len(relative.parts) + 1)):
        raise GenerationError("资产路径不得包含符号链接")
    return path


def extract_zip(source: Path, destination: Path, *, suffixes: set[str]) -> None:
    if source.stat().st_size > MAX_UPLOAD:
        raise GenerationError("压缩包超过 48 MiB")
    private_directory(destination)
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > MAX_FILES or sum(m.file_size for m in members) > MAX_EXPANDED:
                raise GenerationError("压缩包超过 256 个文件或 256 MiB 展开体积")
            seen = set()
            for member in members:
                name = member.filename.rstrip("/")
                path = relative_file(destination, name)
                if name.casefold() in seen:
                    raise GenerationError("压缩包包含重复文件")
                seen.add(name.casefold())
                mode = member.external_attr >> 16
                if member.flag_bits & 1 or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise GenerationError("压缩包不支持加密文件或特殊文件")
                if member.is_dir():
                    private_directory(path)
                    continue
                if path.suffix.lower() not in suffixes:
                    raise GenerationError("压缩包包含不支持的文件类型")
                private_directory(path.parent)
                with archive.open(member) as data, path.open("xb") as target:
                    remaining = member.file_size
                    while chunk := data.read(min(1024 * 1024, remaining + 1)):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise GenerationError("压缩包文件体积不一致")
                        target.write(chunk)
                path.chmod(0o600)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise GenerationError("无法读取 ZIP 压缩包") from exc


def image_info(path: Path) -> tuple[int, int]:
    from PIL import Image

    try:
        with Image.open(path) as img:
            if img.format not in {"PNG", "JPEG", "WEBP"} or img.width * img.height > MAX_PIXELS:
                raise GenerationError("图片仅支持 PNG/JPEG/WebP，最多 16777216 像素")
            size = img.size
            img.verify()
            return size
    except (OSError, Image.DecompressionBombError) as exc:
        raise GenerationError("图片损坏或尺寸超限") from exc


def action_name(value: str, kind: str, index: int) -> str:
    """Use the client's semantic identifier contract without changing model-local names."""
    slug = re.sub(r"[^a-z0-9_-]", "_", value.lower()).strip("_-")
    if not slug or not slug[0].isalpha():
        slug = kind + "_" + slug
    return f"{slug[:54]}_{index}"


def validate_bundle(root: Path) -> dict:
    """Check references and resource bounds, not a substitute for Cubism SDK loading."""
    models = list(root.rglob("*.model3.json"))
    if len(models) != 1:
        raise GenerationError("模型包必须包含唯一的 .model3.json")
    model = models[0]
    base = model.parent
    entries = list(root.rglob("*"))
    if any(p.is_symlink() for p in entries):
        raise GenerationError("模型包不接受符号链接")
    files = [p for p in entries if p.is_file()]
    if len(files) > MAX_FILES or sum(p.stat().st_size for p in files) > MAX_EXPANDED:
        raise GenerationError("模型包体积超限")
    for path in files:
        relative_file(root, path.relative_to(root).as_posix())
        if path.stat().st_nlink != 1:
            raise GenerationError("模型包不接受硬链接文件")
    try:
        doc = json.loads(model.read_text(encoding="utf-8"))
        refs = doc["FileReferences"]
        if doc["Version"] != 3 or not isinstance(refs, dict) or not refs.get("Textures"):
            raise GenerationError("不支持的模型清单")
        names = [refs["Moc"], *refs["Textures"]]
        for key in ("Physics", "Pose", "DisplayInfo", "UserData"):
            if key in refs:
                names.append(refs[key])
        expressions, motions = {}, {}
        expression_names = set()
        for i, entry in enumerate(refs.get("Expressions", [])):
            name = entry["Name"]
            if not isinstance(name, str) or not name or name in expression_names:
                raise GenerationError("表情名称无效或重复")
            expression_names.add(name)
            alias = name if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) else action_name(name, "expression", i)
            if alias in expressions:
                raise GenerationError("表情别名重复")
            expressions[alias] = i
            names.append(entry["File"])
        for group, entries in refs.get("Motions", {}).items():
            if not isinstance(group, str) or not group:
                raise GenerationError("动作组名称无效")
            for i, entry in enumerate(entries):
                motions[action_name(group, "motion", len(motions))] = {"group": group, "index": i}
                names.append(entry["File"])
                if entry.get("Sound"):
                    names.append(entry["Sound"])
        for name in names:
            path = relative_file(base, name)
            if not path.is_file() or not path.stat().st_size:
                raise GenerationError("模型引用文件缺失或为空")
            if path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
        moc = relative_file(base, refs["Moc"])
        with moc.open("rb") as stream:
            header = stream.read(64)
        if len(header) != 64 or header[:4] != b"MOC3" or not 1 <= header[4] <= 5:
            raise GenerationError("模型缺少有效的 MOC3 文件头")
        for texture in refs["Textures"]:
            image_info(relative_file(base, texture))
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise GenerationError("模型清单或引用 JSON 格式无效") from exc
    hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(files)}
    manifest = root / "generation.json"
    if manifest.is_file():
        try:
            declared = json.loads(manifest.read_text(encoding="utf-8"))
            if (declared.get("format") != "agentstrata-live2d-v1"
                    or declared.get("model") != model.relative_to(root).as_posix()
                    or declared.get("files") != {k: v for k, v in hashes.items() if k != "generation.json"}):
                raise GenerationError("生成模型清单与文件摘要不一致")
        except (ValueError, AttributeError) as exc:
            if isinstance(exc, GenerationError):
                raise
            raise GenerationError("生成模型清单无效") from None
    return {"model": model.relative_to(root).as_posix(), "expressions": expressions,
            "motions": motions, "validation": "file-integrity",
            "sha256": hashes}


def make_archive(root: Path, destination: Path) -> None:
    temporary = destination.with_suffix(".partial")
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        if temporary.stat().st_size > MAX_UPLOAD:
            raise GenerationError("完成包超过 48 MiB，请降低输入分辨率")
        temporary.chmod(0o600)
        temporary.rename(destination)
    finally:
        temporary.unlink(missing_ok=True)


class JobRepository:
    def __init__(self, root: Path):
        self.root = private_directory(root)

    def path(self, identifier: str) -> Path:
        return relative_file(self.root, job_id(identifier))

    def create(self, request: dict, source: Path | None) -> str:
        if source is not None and (source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_UPLOAD):
            raise GenerationError("输入文件不存在、是符号链接或超过 48 MiB")
        identifier = uuid4().hex
        root = private_directory(self.path(identifier))
        if source is not None:
            shutil.copyfile(source, root / ("input" + source.suffix.lower()))
        write_json(root / "request.json", request)
        self.save(identifier, {"id": identifier, "name": request["name"], "status": "queued", "stage": "queued"})
        return identifier

    def read(self, identifier: str) -> dict:
        return json.loads((self.path(identifier) / "job.json").read_text(encoding="utf-8"))

    def save(self, identifier: str, value: dict) -> None:
        write_json(self.path(identifier) / "job.json", value)

    def list(self) -> list[dict]:
        return [self.read(p.name) for p in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime,
                                                 reverse=True) if re.fullmatch(r"[a-f0-9]{32}", p.name)
                and p.is_dir() and not p.is_symlink() and (p / "job.json").is_file()]

    @contextmanager
    def exclusive(self):
        path = self.root / ".worker.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise GenerationError("该数据目录已有生成器进程") from None
            yield
