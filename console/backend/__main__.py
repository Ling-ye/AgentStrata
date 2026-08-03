"""python -m console.backend — 启动控制台后端（localhost）。

用法：
  python -m console.backend                 # 默认 127.0.0.1:8910
  python -m console.backend --port 9000
  CHATCOPILOT_CONSOLE_HOST=0.0.0.0 python -m console.backend   # 慎用：暴露内网
"""
from __future__ import annotations

import argparse
import os

from console.bootstrap import ensure_src_path


def main(argv: list[str] | None = None) -> int:
    ensure_src_path()
    parser = argparse.ArgumentParser(prog="python -m console.backend")
    parser.add_argument("--host", default=os.environ.get("CHATCOPILOT_CONSOLE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CHATCOPILOT_CONSOLE_PORT", "8910")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        "console.backend.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
