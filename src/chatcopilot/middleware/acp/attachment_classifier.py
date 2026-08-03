"""ACP resource block classifier.

把"一个 ACP/cc-connect resource block 究竟是不是真实文件附件"这个判定
独立成单一职责的分类器，所有下游路径都只信任 :func:`classify_resource_block`
的结果，不再各自维护字段名硬编码 + 黑名单兜底。

历史背景
========
此前 ``attachment_pipeline.py`` 用 ``_RESOURCE_SOURCE_KEYS = ('path', 'uri',
'url')`` + ``_RESOURCE_NAME_KEYS`` + ``_block_has_web_url_source`` +
两套并行的 ``_pick_resource_name_from_{mapping,object}``。每次飞书
cc-connect 上游加一个字段名（``href`` / ``target`` / 嵌套 ``source.uri``）
或者干脆只在 ``name`` 里塞一个裸 hostname（``example.feishu.cn``），都
要在两处加补丁，导致同一个"URL 被识别为附件文件名"的 bug 反复出现。

设计原则
========
- **白名单优先**：判定文件名走"已知扩展名白名单 → IANA TLD 黑名单 →
  hostname 形态拒绝"三层；未知扩展且不像 hostname 才容错放行。
- **结构对称**：dict / pydantic model 统一走 :func:`_iter_field` 抽象，
  不再维护两套实现。
- **判定单点**：所有 source-like / type 集合在这里一处声明，文件类型扩展
  名只在 :data:`_KNOWN_FILE_EXTENSIONS` 一处维护。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import unquote


class ResourceKind(str, Enum):
    """单个 ACP block 的判定结果。"""

    FILE = "file"
    WEB_URL = "web_url"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedResource:
    """单个 block 的分类结论。

    ``name`` 仅当 ``kind == ResourceKind.FILE`` 时有意义；其余两种 kind 一律
    保留为空字符串，避免误用。
    """

    kind: ResourceKind
    name: str = ""

    @property
    def is_file(self) -> bool:
        return self.kind is ResourceKind.FILE and bool(self.name)


# ---------------------------------------------------------------------------
# 字段集合与白名单
# ---------------------------------------------------------------------------

# ACP block 中"指向资源源头"的字段名。要尽量覆盖 cc-connect / 飞书 / zed
# 等不同上游的命名习惯，新增一个上游字段只在这里加一项。
_SOURCE_FIELD_KEYS: tuple[str, ...] = (
    "path",
    "uri",
    "url",
    "href",
    "source",
    "link",
    "target",
    "file_path",
)

# 名称类字段（按优先级）：上游对人类可读名常用这些 key。
_NAME_FIELD_KEYS: tuple[str, ...] = ("name", "filename", "file_name", "title")

# 嵌套容器：source / resource / location 内部还可能再嵌一层 path / uri。
_NESTED_SOURCE_KEYS: tuple[str, ...] = ("resource", "source", "location")

# Block 的 ``type`` 字段中显式表示"这是一条链接而非文件"的取值。
_NON_FILE_BLOCK_TYPES: frozenset[str] = frozenset(
    {"link", "url", "web_link", "web_url", "bookmark", "hyperlink"}
)

# Web/控制类 URL scheme：path/uri/url 解析为这些 scheme → 一律非文件。
_NON_FILE_URL_SCHEMES: frozenset[str] = frozenset(
    {
        "http",
        "https",
        "ftp",
        "ftps",
        "sftp",
        "ws",
        "wss",
        "mailto",
        "tel",
        "sms",
        "data",
        "about",
        "javascript",
        "blob",
    }
)
_URL_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*):")

# 真实文件路径的强信号：file://、绝对路径（Windows 盘符 / POSIX 根 / UNC）。
_FILE_SOURCE_RE = re.compile(
    r"^(?:file://|[A-Za-z]:[\\/]|/[A-Za-z0-9._-]|\\\\)"
)

# 已知文件扩展名白名单。新增类型只在这里加；其余未知扩展走 hostname 容错判定。
_KNOWN_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        # 数据/归档
        "csv", "json", "jsonl", "tsv", "parquet", "feather",
        "zip", "tar", "tgz", "gz", "bz2", "xz", "7z", "rar",
        # 内存分析自有格式
        "memory_report", "modulealloc", "monotree", "vma",
        # 日志/文本
        "log", "txt", "md", "rst", "ini", "cfg", "conf",
        # 标记语言
        "html", "htm", "xml", "yaml", "yml", "toml",
        # 图像
        "png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "ico", "tiff",
        # 文档
        "pdf", "xlsx", "xls", "docx", "doc", "pptx", "ppt", "odt", "ods",
        # 音视频（用户可能上传录屏）
        "mp4", "mov", "mkv", "webm", "mp3", "wav", "ogg", "flac",
        # 源代码（部分场景会发代码片段附件）
        "py", "ts", "tsx", "js", "jsx", "rs", "go", "java", "kt", "cs",
        "cpp", "cc", "c", "h", "hpp", "sh", "bat", "ps1",
    }
)

# 顶级域名（IANA TLD 高频集合）。若一个裸 name 的"扩展名"落在这里，且整串
# 看着像 hostname（多段 ASCII + dots），就拒绝识别为文件。覆盖最常见的
# 公司/通用 TLD，不需要完整 IANA 列表。
_NON_FILE_TLDS: frozenset[str] = frozenset(
    {
        "com", "cn", "net", "org", "io", "app", "dev", "co", "us", "uk",
        "jp", "kr", "de", "fr", "ru", "br", "in", "au", "ca", "es", "it",
        "nl", "se", "no", "fi", "dk", "pl", "ch", "be", "tw", "hk", "sg",
        "info", "biz", "name", "pro", "mobi", "asia", "ai", "tech", "site",
        "store", "online", "shop", "blog", "club", "live", "work", "tv",
        "cc", "me", "ly", "to", "xyz", "icu", "top", "vip",
        "gov", "edu", "mil", "int",
    }
)

# cc-connect 内部目录段（在文本路径里出现过把这些当成"文件名"的假阳性）。
_INTERNAL_PATH_SEGMENTS: frozenset[str] = frozenset(
    {".cc-connect", "cc-connect", ".lark-cli", "attachments", "downloads", "uploads", "results"}
)

# Hostname 形态：多段、纯 ASCII 字母数字 + `-`、用 `.` 分隔（如 ``a.b.cn``）。
# 不允许任何 ``/`` ``\`` ``@`` ``:``，避免把 URL 片段也归到这里。
_HOSTNAME_LIKE_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?){1,}$"
)


# ---------------------------------------------------------------------------
# 字段访问抽象
# ---------------------------------------------------------------------------

def _iter_field(block: Any, key: str) -> Any:
    """同时支持 dict 和带 attribute 的对象（pydantic model）。"""
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def _stringify_scalar(value: Any) -> str:
    """把 source 字段值规整为字符串：dict/list/None 一律返回空串。"""
    if value is None:
        return ""
    if isinstance(value, (str, bytes)):
        return value.decode() if isinstance(value, bytes) else value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _looks_like_web_url(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate:
        return False
    match = _URL_SCHEME_RE.match(candidate)
    if not match:
        return False
    return match.group("scheme").lower() in _NON_FILE_URL_SCHEMES


def _looks_like_file_source(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate:
        return False
    return bool(_FILE_SOURCE_RE.match(candidate))


def resource_basename(value: str) -> str:
    """把 ``file://`` / Windows / POSIX 路径规整为纯 basename。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    if re.match(r"^/[A-Za-z]:/", raw):
        raw = raw[1:]
    raw = unquote(raw)
    return raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# basename 白名单判定
# ---------------------------------------------------------------------------

def _split_extension(name: str) -> tuple[str, str]:
    """返回 (head, ext.lower())。无扩展或全是点 → ext 为空串。"""
    candidate = (name or "").strip()
    head, sep, ext = candidate.rpartition(".")
    if not sep or not head or not ext:
        return candidate, ""
    return head, ext.lower()


def _is_hostname_like(name: str) -> bool:
    candidate = (name or "").strip()
    if not candidate:
        return False
    if "/" in candidate or "\\" in candidate or "@" in candidate or ":" in candidate:
        return False
    return bool(_HOSTNAME_LIKE_RE.match(candidate))


def _classify_basename(name: str) -> ResourceKind:
    """单点的 basename → ResourceKind 判定。

    优先级：
    1. 空 / 内部目录段 / dotfile → :attr:`ResourceKind.UNKNOWN`
    2. 整串本身就是 web URL → :attr:`ResourceKind.WEB_URL`
    3. 扩展名 ∈ :data:`_KNOWN_FILE_EXTENSIONS` → :attr:`ResourceKind.FILE`
    4. 扩展名 ∈ :data:`_NON_FILE_TLDS` 且整串 hostname-like → :attr:`ResourceKind.WEB_URL`
    5. 整串 hostname-like（覆盖未列入 TLD 黑名单的边角域名） → :attr:`ResourceKind.WEB_URL`
    6. 有任意扩展名（不在两个名单里，也不像 hostname）→ :attr:`ResourceKind.FILE`（容错）
    7. 没有扩展名 → :attr:`ResourceKind.UNKNOWN`
    """
    candidate = (name or "").strip()
    if not candidate:
        return ResourceKind.UNKNOWN
    if candidate in _INTERNAL_PATH_SEGMENTS:
        return ResourceKind.UNKNOWN
    if candidate.startswith(".cc-"):
        return ResourceKind.UNKNOWN
    if _looks_like_web_url(candidate):
        return ResourceKind.WEB_URL

    _head, ext = _split_extension(candidate)
    if not ext:
        return ResourceKind.UNKNOWN
    if ext in _KNOWN_FILE_EXTENSIONS:
        return ResourceKind.FILE
    if ext in _NON_FILE_TLDS and _is_hostname_like(candidate):
        return ResourceKind.WEB_URL
    if _is_hostname_like(candidate):
        return ResourceKind.WEB_URL
    return ResourceKind.FILE


def is_plausible_file_basename(name: str) -> bool:
    """供文本兜底正则调用的 public 入口，保持与 classifier 同源判定。"""
    return _classify_basename(name) is ResourceKind.FILE


# ---------------------------------------------------------------------------
# block 字段提取
# ---------------------------------------------------------------------------

def _collect_source_values(block: Any) -> list[str]:
    """递归 1 层抽取 block 的所有 source-like 字段为字符串列表。"""
    values: list[str] = []

    for key in _SOURCE_FIELD_KEYS:
        raw = _iter_field(block, key)
        scalar = _stringify_scalar(raw)
        if scalar:
            values.append(scalar)
            continue
        # source/link/target 也可能是 {"uri": "..."} 这种内嵌结构
        if isinstance(raw, dict) or hasattr(raw, "__dict__") or hasattr(raw, "__class__"):
            for inner_key in _SOURCE_FIELD_KEYS:
                inner = _stringify_scalar(_iter_field(raw, inner_key))
                if inner:
                    values.append(inner)

    for nested_key in _NESTED_SOURCE_KEYS:
        nested = _iter_field(block, nested_key)
        if nested is None:
            continue
        for inner_key in _SOURCE_FIELD_KEYS:
            inner = _stringify_scalar(_iter_field(nested, inner_key))
            if inner:
                values.append(inner)

    return values


def _pick_bare_name(block: Any) -> str:
    """从 name/title 系列字段取一个非空裸名（**不**回退到 source 字段）。"""
    for key in _NAME_FIELD_KEYS:
        raw = _stringify_scalar(_iter_field(block, key))
        if raw:
            return raw.strip()
    return ""


def _read_block_type(block: Any) -> str:
    return (_stringify_scalar(_iter_field(block, "type")) or "").strip().lower()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def classify_resource_block(block: Any) -> ClassifiedResource:
    """判定一个 ACP block 究竟是不是真实文件附件。

    判定顺序（白名单优先）：

    1. block ``type`` ∈ :data:`_NON_FILE_BLOCK_TYPES` → :attr:`ResourceKind.WEB_URL`
    2. 任意 source-like 字段解析为 web URL → :attr:`ResourceKind.WEB_URL`
    3. 任意 source-like 字段解析为 ``file://`` / 绝对路径，且 basename 通过
       :func:`_classify_basename` → :attr:`ResourceKind.FILE`
    4. 否则取 name/title 走 :func:`_classify_basename`（结果可能是
       :attr:`ResourceKind.FILE` / :attr:`ResourceKind.WEB_URL` /
       :attr:`ResourceKind.UNKNOWN`）

    返回 :class:`ClassifiedResource`。
    """
    if block is None:
        return ClassifiedResource(ResourceKind.UNKNOWN)

    block_type = _read_block_type(block)
    if block_type in _NON_FILE_BLOCK_TYPES:
        return ClassifiedResource(ResourceKind.WEB_URL)

    sources = _collect_source_values(block)
    for src in sources:
        if _looks_like_web_url(src):
            return ClassifiedResource(ResourceKind.WEB_URL)

    for src in sources:
        if _looks_like_file_source(src):
            basename = resource_basename(src)
            kind = _classify_basename(basename)
            if kind is ResourceKind.FILE:
                return ClassifiedResource(ResourceKind.FILE, basename)
            # file:// 但 basename 又像 hostname / 内部目录段 → 保守拒绝
            return ClassifiedResource(kind)

    bare = _pick_bare_name(block)
    if not bare:
        return ClassifiedResource(ResourceKind.UNKNOWN)

    kind = _classify_basename(bare)
    if kind is ResourceKind.FILE:
        return ClassifiedResource(ResourceKind.FILE, resource_basename(bare))
    return ClassifiedResource(kind)


__all__ = [
    "ResourceKind",
    "ClassifiedResource",
    "classify_resource_block",
    "is_plausible_file_basename",
    "resource_basename",
]
