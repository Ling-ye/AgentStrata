"""Resolve a frozen application into one category-neutral packaging plan."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any

from chatcopilot.core.file_integrity import trusted_source_sha256


WINDOWS = "windows-x64"
LINUX = "linux-x64"
FILES = {WINDOWS: "application.exe", LINUX: "application.run"}
SCHEMA = "agentstrata-install/v1"

PUBLIC_RECIPES = frozenset({"static", "client", "service"})
HOST_RECIPES = frozenset({"native", "gateway"})
RECIPES = PUBLIC_RECIPES | HOST_RECIPES
_FIELDS = frozenset(
    {"schema", "recipe", "platforms", "entry", "clients", "requirements", "image_licenses"}
)
_APPLICATION_ID = re.compile(r"[a-z][a-z0-9-]{0,79}")


@dataclass(frozen=True)
class PackagePlan:
    recipe: str
    platforms: tuple[str, ...]
    clients: Mapping[str, str]
    requirements: tuple[str, ...]
    docker: bool
    spec: Mapping[str, Any]
    descriptor: str | None
    prefix: str

    def select_targets(self, requested: Iterable[str] | None) -> tuple[str, ...]:
        if requested is None:
            return self.platforms
        values = _strings(requested, "安装平台")
        if not values or len(set(values)) != len(values) or not set(values).issubset(self.platforms):
            raise ValueError("所选版本不支持这个安装平台")
        return values


def resolve_plan(
    application: str,
    members: Mapping[str, Mapping[str, Any]] | Iterable[str],
    spec: Mapping[str, Any] | None = None,
    *,
    application_dir: str | None = None,
    default_recipe: str | None = None,
    default_platforms: Iterable[str] | None = None,
    default_requirements: Iterable[str] = (),
) -> PackagePlan:
    """Use the same member-based decision for Git preview and frozen builds."""

    records, names = _members(members)
    prefix = application_prefix(application, application_dir)
    descriptor, compose, index = (prefix + name for name in ("install.json", "compose.yaml", "index.html"))
    has_descriptor = descriptor in names
    if (spec is None) != (not has_descriptor):
        raise ValueError("安装声明与已选源码不一致")
    declaration = _declaration(spec)
    explicit = declaration.get("recipe")
    if explicit is not None and (
        not isinstance(explicit, str) or explicit not in PUBLIC_RECIPES
    ):
        raise ValueError("install.json 只能声明 static、client 或 service 配方")
    if default_recipe is not None and default_recipe not in RECIPES:
        raise ValueError("宿主选择了不支持的打包配方")

    clients = _clients(declaration.get("clients", {}), prefix, names, records)
    discovered = None
    if compose in names:
        discovered = "service"
    elif clients:
        discovered = "client"
    elif index in names and default_recipe != "native":
        discovered = "static"
    if default_recipe == "gateway":
        if explicit is not None:
            raise ValueError("安装声明不能覆盖宿主选择的 Gateway 配方")
        recipe = "gateway"
    else:
        recipe = explicit or discovered or default_recipe
    if recipe is None:
        raise ValueError("源码没有可发现的安装入口，请声明打包配方")
    if explicit is not None and discovered is not None and explicit != discovered:
        raise ValueError("install.json 配方与应用源码入口冲突")
    if recipe == "service" and (compose not in names or not has_descriptor):
        raise ValueError("服务配方需要 install.json 和 compose.yaml")
    if recipe == "client" and not clients:
        raise ValueError("客户端配方缺少平台启动文件")
    if recipe == "static" and index not in names:
        raise ValueError("静态网页配方需要当前应用的 index.html")
    if recipe == "gateway" and f"bots/{application}/bot.yaml" not in names:
        raise ValueError("Gateway 配方缺少当前应用的 BotSpec")

    platforms = _platforms(declaration.get("platforms"), clients, recipe, default_platforms)
    if recipe == "native" and platforms != (WINDOWS,):
        raise ValueError("原生桌面应用只支持 Windows x64")
    if recipe not in {"service", "gateway"} and (declaration.get("entry") is not None or declaration.get("image_licenses")):
        raise ValueError("服务入口和镜像许可需要独立服务配方")
    if clients and not set(clients).issubset(platforms):
        raise ValueError("客户端入口超出声明的安装平台")
    if recipe == "client" and set(clients) != set(platforms):
        raise ValueError("客户端配方必须为每个安装平台声明启动文件")
    requirements = _strings(default_requirements, "接收方准备项") + _strings(
        declaration.get("requirements", ()), "接收方准备项"
    )
    return PackagePlan(
        recipe=recipe,
        platforms=platforms,
        clients=clients,
        requirements=tuple(dict.fromkeys(requirements)),
        docker=recipe in {"service", "gateway"},
        spec=declaration,
        descriptor=descriptor if has_descriptor else None,
        prefix=prefix,
    )


def read_plan(
    source: Path,
    manifest: Mapping[str, Mapping[str, Any]],
    application: str,
    *,
    application_dir: str | None = None,
    **defaults: Any,
) -> PackagePlan:
    """Verify and read frozen ``install.json``, then delegate to ``resolve_plan``."""

    source = source.absolute()
    if source.resolve() != source:
        raise ValueError("打包源码目录必须是规范路径")
    descriptor = application_prefix(application, application_dir) + "install.json"
    spec = None
    if descriptor in manifest:
        record = manifest[descriptor]
        expected = record.get("sha256") if isinstance(record, Mapping) else None
        if not isinstance(expected, str) or re.fullmatch(r"[a-f0-9]{64}", expected) is None:
            raise ValueError("安装声明的源码摘要无效")
        path = source / descriptor
        size = path.lstat().st_size
        if trusted_source_sha256(path, root=source, max_bytes=max(1, size)) != expected:
            raise ValueError("install.json 与已选源码的摘要不符")
        try:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("install.json 在读取时发生变化")
            spec = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("install.json 不是有效的 UTF-8 JSON") from None
    return resolve_plan(
        application, manifest, spec, application_dir=application_dir, **defaults
    )


def application_prefix(application: str, application_dir: str | None = None) -> str:
    if not isinstance(application, str) or _APPLICATION_ID.fullmatch(application) is None:
        raise ValueError("应用标识不能用于生成安装包")
    value = f"integrations/{application}" if application_dir is None else application_dir
    if value == ".":
        return ""
    path = PurePosixPath(value) if isinstance(value, str) else None
    if (
        path is None
        or "\\" in value
        or PureWindowsPath(value).drive
        or path.as_posix() != value
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("应用打包目录必须是项目内的规范相对路径")
    return value + "/"


def _members(
    members: Mapping[str, Mapping[str, Any]] | Iterable[str],
) -> tuple[Mapping[str, Mapping[str, Any]], frozenset[str]]:
    records: Mapping[str, Mapping[str, Any]] = members if isinstance(members, Mapping) else {}
    raw = members.keys() if isinstance(members, Mapping) else members
    if isinstance(raw, (str, bytes)):
        raise ValueError("安装源码成员必须是路径集合")
    names = []
    for name in raw:
        path = PurePosixPath(name) if isinstance(name, str) else None
        if (
            path is None
            or path.as_posix() != name
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("安装源码成员名称无效")
        names.append(name)
    return records, frozenset(names)


def _declaration(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError("install.json 必须是对象")
    result = dict(value)
    unknown = set(result) - _FIELDS
    if unknown:
        raise ValueError("install.json 含有未支持的字段：" + ", ".join(sorted(unknown)))
    schema = result.get("schema")
    if schema is not None and schema != SCHEMA:
        raise ValueError(f"不支持的 install.json schema：{schema}")
    return result


def _platforms(
    explicit: Any,
    clients: Mapping[str, str],
    recipe: str,
    defaults: Iterable[str] | None,
) -> tuple[str, ...]:
    value = explicit if explicit is not None else clients.keys() if clients else defaults
    if value is None:
        return (WINDOWS,) if recipe == "native" else tuple(FILES)
    raw = _strings(value, "安装平台")
    seen = set()
    for platform in raw:
        if platform not in FILES or platform in seen:
            raise ValueError("安装平台包含重复或不支持的值")
        seen.add(platform)
    if not seen:
        raise ValueError("安装平台不能为空")
    return tuple(platform for platform in FILES if platform in seen)


def _clients(
    value: Any,
    prefix: str,
    names: frozenset[str],
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    if not isinstance(value, Mapping) or any(
        not isinstance(platform, str) or platform not in FILES for platform in value
    ):
        raise ValueError("客户端入口请按支持的操作系统声明")
    result = {}
    for platform, relative in value.items():
        path = PurePosixPath(relative) if isinstance(relative, str) else None
        if (
            path is None
            or not relative.isprintable()
            or "\\" in relative
            or path.as_posix() != relative
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or (platform == WINDOWS and path.suffix.lower() not in {".exe", ".html", ".htm"})
        ):
            raise ValueError("客户端必须指向本应用公开目录中的可启动文件")
        member = prefix + relative
        if member not in names:
            raise ValueError("客户端启动文件不属于已选源码")
        metadata = records.get(member)
        if (
            platform == LINUX
            and path.suffix.lower() not in {".html", ".htm"}
            and isinstance(metadata, Mapping)
            and metadata.get("executable") is not True
        ):
            raise ValueError("Linux 客户端入口没有可执行权限")
        result[platform] = relative
    return result


def _strings(value: Iterable[Any], label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(label + "必须是数组")
    try:
        result = tuple(value)
    except TypeError:
        raise ValueError(label + "必须是数组") from None
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise ValueError(label + "包含空值或非字符串")
    return tuple(item.strip() for item in result)
