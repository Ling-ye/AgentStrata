export type HealthColor = "green" | "yellow" | "red" | "grey" | "gray" | "";
export type TagColor = "green" | "orange" | "red" | "gray" | "blue" | "cyan" | "purple" | "light-blue";

export function healthTagColor(color: HealthColor): TagColor {
  if (color === "yellow") return "orange";
  if (color === "grey" || color === "gray" || !color) return "gray";
  return color;
}

export function infraStateLabel(state: string): string {
  const labels: Record<string, string> = {
    healthy: "健康",
    running: "运行中",
    stopped: "已停止",
    unhealthy: "异常",
    not_found: "未找到",
    enabled: "已启用",
  };
  return labels[state] || state;
}

export function severityTagColor(severity: "critical" | "warning" | "info"): TagColor {
  if (severity === "critical") return "red";
  if (severity === "warning") return "orange";
  return "blue";
}

export function severityLabel(severity: "critical" | "warning" | "info"): string {
  const labels = {
    critical: "严重",
    warning: "警告",
    info: "提示",
  };
  return labels[severity];
}
