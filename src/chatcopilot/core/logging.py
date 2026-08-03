"""Logging helpers for platform entry points.

整个进程的 logging 入口集中在这里。设计目标：

1. **stderr 主流**：所有 ``chatcopilot.*`` logger 走 stderr，便于 cc-connect 抓取并
   并入 ``/tmp/cc-connect.log``（→ symlink → ``$CHATCOPILOT_LOG_DIR/cc-connect/<date>.log``）。
2. **独立 runtime 文件**：当 ``CHATCOPILOT_LOG_DIR`` 已设置时，额外写一份
   ``$CHATCOPILOT_LOG_DIR/runtime/<date>.log``，仅收 ``chatcopilot.*`` logger，方便事后只
   看本项目自身的事件，不被外部依赖（litellm / openai / urllib3 等）的 INFO 噪声淹没。
3. **幂等**：``configure_logging()`` 可以被反复调用——第二次起会原地更新 level，但不会
   重复挂 handler（避免 ACP / MCP / 测试 fixture 多入口反复 ``basicConfig`` 把 chatcopilot
   logger 输出复制 N 份）。
4. **fail-open**：FileHandler 创建失败（路径不可写、磁盘满等）只 warn 一次到 stderr，
   不抛异常拖死 ACP runtime 或 cc-connect 拉起的子进程。
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from chatcopilot.project import ENV_PREFIX

_LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s |%(correlation)s %(message)s"

# 标识本模块加上去的 handler，让 configure_logging 可幂等地识别并复用，避免重复挂载。
_STREAM_HANDLER_ATTR = "_chatcopilot_stream_handler"
_FILE_HANDLER_ATTR = "_chatcopilot_file_handler"


class _ChatcopilotOnlyFilter(logging.Filter):
    """只让 ``chatcopilot.*`` 命名空间的记录通过。

    ACP runtime 在导入 litellm / openai / urllib3 / asyncio 之类依赖后，root logger
    会变得非常吵；runtime/<date>.log 应当只反映本项目自身的事件，不混入外部库的 INFO
    级噪声，这样人工 grep 时直接对应到 ``chatcopilot/...`` 模块。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "chatcopilot" or record.name.startswith("chatcopilot.")


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        from chatcopilot.core.log_context import current_log_context

        context = current_log_context()
        parts = [f"{key}={context[key]}" for key in ("task_id", "trace_id", "session_id", "job_id") if context.get(key)]
        record.correlation = (" " + " ".join(parts) + " |") if parts else ""
        return True


def _resolve_level(default_level: str, *extra_env_keys: str) -> int:
    for key in (*extra_env_keys, f"{ENV_PREFIX}_LOG_LEVEL"):
        value = os.environ.get(key)
        if value:
            level = getattr(logging, value.upper(), None)
            if isinstance(level, int):
                return level
    return getattr(logging, default_level.upper(), logging.INFO)


def _make_stream_handler() -> logging.Handler:
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(_CorrelationFilter())
    setattr(handler, _STREAM_HANDLER_ATTR, True)
    return handler


def _make_file_handler(log_dir: Path) -> Optional[logging.Handler]:
    runtime_dir = log_dir / "runtime"
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        target = runtime_dir / f"{date.today().isoformat()}.log"
        handler = logging.FileHandler(target, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(
            f"[chatcopilot.core.logging] WARN: cannot open runtime log "
            f"under {runtime_dir}: {exc}\n"
        )
        return None
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(_ChatcopilotOnlyFilter())
    handler.addFilter(_CorrelationFilter())
    setattr(handler, _FILE_HANDLER_ATTR, True)
    return handler


def _find_marker(handlers, attr: str) -> Optional[logging.Handler]:
    for handler in handlers:
        if getattr(handler, attr, False):
            return handler
    return None


def configure_logging(default_level: str = "INFO", *extra_env_keys: str) -> None:
    """Configure logging once for CLI / runtime entry points.

    幂等：反复调用只会更新 level，不会重复挂 handler。

    :param default_level: 调用方传进的兜底 level（``INFO`` / ``DEBUG`` / ...）。
    :param extra_env_keys: 入口专属的 level env 名（如 ``CHATCOPILOT_ACP_LOG_LEVEL`` /
        ``CHATCOPILOT_MCP_LOG_LEVEL``）。先于 ``CHATCOPILOT_LOG_LEVEL`` 生效。
    """

    level = _resolve_level(default_level, *extra_env_keys)
    root = logging.getLogger()
    root.setLevel(level)

    stream_handler = _find_marker(root.handlers, _STREAM_HANDLER_ATTR)
    if stream_handler is None:
        stream_handler = _make_stream_handler()
        root.addHandler(stream_handler)
    stream_handler.setLevel(level)

    log_dir_value = os.environ.get(f"{ENV_PREFIX}_LOG_DIR")
    if log_dir_value:
        file_handler = _find_marker(root.handlers, _FILE_HANDLER_ATTR)
        if file_handler is None:
            new_file_handler = _make_file_handler(Path(log_dir_value).expanduser())
            if new_file_handler is not None:
                new_file_handler.setLevel(level)
                root.addHandler(new_file_handler)
        else:
            file_handler.setLevel(level)
