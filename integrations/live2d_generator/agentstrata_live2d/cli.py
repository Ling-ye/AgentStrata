"""Human-operated generator commands and its private process entry point."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from .runtime import configured_runtime, worker
from .types import GenerationError, GenerationRequest, TERMINAL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentstrata-live2d")
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("serve", "generate"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--root", type=Path, required=True, help="Private generation job directory")
        if name == "generate":
            command.add_argument("--name", required=True)
            command.add_argument("--prompt", default="")
            command.add_argument("--information", type=Path, help="JSON text fields describing the character")
            command.add_argument("--input", type=Path)
            command.add_argument("--redraw", action="store_true")
    for name in ("install", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--vtuber-root", type=Path, required=True)
        if name == "install":
            command.add_argument("--bundle", type=Path, required=True)
        else:
            command.add_argument("--backup", required=True)
    child = commands.add_parser("_worker", help=argparse.SUPPRESS)
    child.add_argument("--root", type=Path, required=True)
    child.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "_worker":
            return worker(args.root, args.job)
        if args.operation in {"install", "restore"}:
            from .install import install_bundle, restore_bundle
            result = (install_bundle(args.bundle, args.vtuber_root) if args.operation == "install"
                      else restore_bundle(args.vtuber_root, args.backup))
        else:
            runtime = configured_runtime(args.root, args.config)
            if args.operation == "serve":
                import uvicorn
                from .web import create_app
                uvicorn.run(create_app(runtime), host="127.0.0.1", port=runtime.config.port, log_level="warning")
                return 0
            request = GenerationRequest.parse({"name": args.name, "prompt": args.prompt,
                "information": json.loads(args.information.read_text()) if args.information else {},
                "redraw": args.redraw})
            with runtime:
                state = runtime.start(request, args.input)
                identifier, last_stage = state["id"], ""
                while state["status"] not in TERMINAL:
                    if state["stage"] != last_stage:
                        print(f"[{identifier[:8]}] {state['stage']}", file=sys.stderr, flush=True)
                        last_stage = state["stage"]
                    time.sleep(0.5)
                    state = runtime.get(identifier)
                result = state
                if state["status"] == "completed":
                    result["bundle"] = str(runtime.artifact(identifier, "model.zip"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") in {"failed", "cancelled"} else 0
    except GenerationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("生成已中断", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError) as exc:
        print(f"操作失败（{type(exc).__name__}）；检查文件和配置", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
