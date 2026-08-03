"""平台适配器的目录扫描自动发现。

扫描 ``platforms/`` 下每个子包的 ``adapter.py``，收集模块级 ``ADAPTER``
（:class:`PlatformAdapter` 实例），按 ``adapter.name`` 建表。新增平台只需在
``platforms/<name>/adapter.py`` 暴露 ``ADAPTER``，无需改任何注册表或白名单。

该表是平台是否“被支持”的**唯一来源**：``botspec.loader`` 校验、``platforms.router``
门面、CLI 与部署脚本全部从这里取。
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from typing import Dict

from chatcopilot.platforms.base import PlatformAdapter


class UnsupportedPlatformError(RuntimeError):
    """BotSpec 声明了一个 registry 还没发现的平台类型。"""


_ADAPTER_ATTR = "ADAPTER"
_REGISTRY_LOCK = threading.Lock()
_REGISTRY_CACHE: Dict[str, PlatformAdapter] | None = None


def _discover() -> Dict[str, PlatformAdapter]:
    """遍历 ``chatcopilot.platforms`` 的子包，import 含 ``adapter`` 的子模块。"""
    import chatcopilot.platforms as _platforms_pkg

    registry: Dict[str, PlatformAdapter] = {}
    for module_info in pkgutil.iter_modules(_platforms_pkg.__path__):
        if not module_info.ispkg:
            continue
        adapter_module_name = f"{_platforms_pkg.__name__}.{module_info.name}.adapter"
        try:
            module = importlib.import_module(adapter_module_name)
        except ModuleNotFoundError:
            # 不是所有子包都提供 adapter（如 prompts 资源目录）；静默跳过。
            continue
        adapter = getattr(module, _ADAPTER_ATTR, None)
        if not isinstance(adapter, PlatformAdapter):
            continue
        key = (adapter.name or "").strip().lower()
        if not key:
            raise RuntimeError(f"{adapter_module_name}.ADAPTER 缺少非空 name")
        if key in registry:
            raise RuntimeError(
                f"平台名冲突：{key!r} 同时由 {registry[key].__class__.__name__} 与 "
                f"{adapter.__class__.__name__} 声明"
            )
        registry[key] = adapter
    return registry


def _registry() -> Dict[str, PlatformAdapter]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        with _REGISTRY_LOCK:
            if _REGISTRY_CACHE is None:
                _REGISTRY_CACHE = _discover()
    return _REGISTRY_CACHE


def reset_cache() -> None:
    """清空发现缓存（仅供测试在动态增删平台后重新扫描）。"""
    global _REGISTRY_CACHE
    with _REGISTRY_LOCK:
        _REGISTRY_CACHE = None


def get_adapter(platform_type: str) -> PlatformAdapter:
    """按 ``platform.type`` 取适配器；未发现则抛 :class:`UnsupportedPlatformError`。"""
    normalized = (platform_type or "").strip().lower()
    adapter = _registry().get(normalized)
    if adapter is None:
        raise UnsupportedPlatformError(
            f"未支持的 platform.type={platform_type!r}；当前可用："
            + ", ".join(supported_platform_types())
        )
    return adapter


def supported_platform_types() -> tuple[str, ...]:
    """返回当前已发现的全部 ``platform.type`` 字符串（已排序）。"""
    return tuple(sorted(_registry()))


def is_supported(platform_type: str) -> bool:
    return (platform_type or "").strip().lower() in _registry()


__all__ = [
    "UnsupportedPlatformError",
    "get_adapter",
    "is_supported",
    "reset_cache",
    "supported_platform_types",
]
