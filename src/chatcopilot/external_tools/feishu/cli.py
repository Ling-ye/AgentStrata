"""通用飞书工具命令行入口。

用法示例:
    python -m chatcopilot.external_tools.feishu.cli --action doc-create --title "周报" --markdown "# 本周\n- ..."
    python -m chatcopilot.external_tools.feishu.cli --action im-send --receive-id ou_xxx --text "hello"
"""
from __future__ import annotations

import sys

from chatcopilot.external_tools.feishu.cmd import build_cli_parser, run_cli


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    return run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
