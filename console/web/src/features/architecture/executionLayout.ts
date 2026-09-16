import type { ElkNode } from "elkjs/lib/elk-api";

export function executionLayoutGraph(nodes: string[], edges: string[][]): ElkNode {
  return { id: "execution", layoutOptions: {
    "elk.algorithm": "layered", "elk.direction": "RIGHT",
    "elk.layered.spacing.nodeNodeBetweenLayers": "90", "elk.spacing.nodeNode": "40",
    // React Flow routes the visible edges. Avoid ELK's expensive orthogonal routing and
    // crossing optimization on high-fanout agent spans; recorded node order stays readable.
    "elk.edgeRouting": "POLYLINE", "elk.layered.nodePlacement.strategy": "SIMPLE",
    "elk.layered.crossingMinimization.strategy": "NONE", "elk.layered.highDegreeNodes.treatment": "true",
  }, children: nodes.map((id) => ({ id, width: 250, height: 130 })),
  edges: edges.map(([id, source, target]) => ({ id, sources: [source], targets: [target] })) };
}
