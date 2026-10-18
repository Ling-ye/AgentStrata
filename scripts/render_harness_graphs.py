"""Render actual compiled Harness topology without executing nodes or opening storage."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def render():
    from chatcopilot.harness.engine.acceptance_graph import build_acceptance_graph
    from chatcopilot.harness.batch_graph import build_batch_graph
    from chatcopilot.harness.delivery_graph import build_delivery_graph
    from chatcopilot.harness.engine.repair_graph import build_task_graph

    # The builders register these functions but rendering never invokes them.
    def node(_name):
        return lambda state: {}

    ports = SimpleNamespace(node=node, graph_node=node, delivery=True, deliver=lambda state: {})
    graphs = (("任务", build_task_graph(None)),
              ("共用验收", build_acceptance_graph()),
              ("交付", build_delivery_graph(ports, None)),
              ("代码熵回收批次", build_batch_graph(ports, None)))
    parts = ["# Harness 编译图\n\n由 `.venv/bin/python scripts/render_harness_graphs.py` 从实际 StateGraph 生成；"
             "使用 `--check` 检查是否与源码一致。这里只展示拓扑，不执行节点。"
             "状态、权限与恢复规则见 [Harness](../../docs/reference/harness.md)。\n"]
    for title, graph in graphs:
        parts.append(f"\n## {title}\n\n```mermaid\n{graph.get_graph().draw_mermaid().strip()}\n```\n")
    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = ROOT / "specs/harness-langgraph/graphs.md"
    content = render()
    if args.check:
        if not path.exists() or path.read_text() != content:
            print("Harness graph diagrams differ; run scripts/render_harness_graphs.py")
            return 1
        print("OK: compiled Harness diagrams")
    else:
        path.write_text(content, encoding="utf-8")
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
